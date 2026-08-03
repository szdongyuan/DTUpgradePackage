import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from control import program_bundle
from control.program_bundle import ProgramBundleError, replace_program_bundle


def _write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


def _bundle_paths(tmp_path):
    source_parent = tmp_path / "package" / "resources"
    destination_parent = tmp_path / "deployed"
    source_parent.mkdir(parents=True)
    destination_parent.mkdir()
    source_executable = _write(source_parent / "DiTing.exe", "new executable")
    return (
        source_executable,
        destination_parent / "DiTing.exe",
        source_parent / "_internal",
        destination_parent / "_internal",
    )


def _replace(paths, **kwargs):
    return replace_program_bundle(*(str(path) for path in paths), **kwargs)


def _make_symlink(path, target, *, directory):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symbolic links unavailable: {error}")


def _make_junction(path, target):
    if os.name != "nt" or not hasattr(Path(path), "is_junction"):
        pytest.skip("junctions are only available on supported Windows versions")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(path), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: {result.stderr or result.stdout}")
    assert Path(path).is_junction()


def _make_directory_alias(path, target):
    if os.name == "nt":
        _make_junction(path, target)
    else:
        _make_symlink(path, target, directory=True)


def _assert_no_transaction_artifacts(destination_parent):
    assert sorted(path.name for path in destination_parent.iterdir() if ".program-bundle-" in path.name) == []


def test_success_resolves_parent_aliases_and_keeps_fixed_leaf_names(tmp_path):
    paths = list(_bundle_paths(tmp_path))
    source_real_parent = paths[0].parent
    destination_real_parent = paths[1].parent
    source_alias = tmp_path / "source-alias"
    destination_alias = tmp_path / "destination-alias"
    _make_directory_alias(source_alias, source_real_parent)
    _make_directory_alias(destination_alias, destination_real_parent)
    paths[0] = source_alias / "DiTing.exe"
    paths[2] = source_alias / "_internal"
    paths[1] = destination_alias / "DiTing.exe"
    paths[3] = destination_alias / "_internal"

    result = _replace(paths)

    assert result.internal_replaced is False
    assert (destination_real_parent / "DiTing.exe").read_text(encoding="utf-8") == "new executable"
    _assert_no_transaction_artifacts(destination_real_parent)


@pytest.mark.parametrize("destination_kind", ["directory", "file", "symlink", "broken_symlink"])
def test_missing_internal_replaces_executable_and_preserves_destination_internal(tmp_path, destination_kind):
    paths = _bundle_paths(tmp_path)
    destination_internal = paths[3]
    external = tmp_path / "external-internal"
    if destination_kind == "directory":
        _write(destination_internal / "old.txt", "old internal")
    elif destination_kind == "file":
        _write(destination_internal, "old internal file")
    elif destination_kind == "symlink":
        _write(external / "old.txt", "external old")
        _make_symlink(destination_internal, external, directory=True)
    else:
        _make_symlink(destination_internal, tmp_path / "missing-external", directory=True)
    original_lstat = destination_internal.lstat()

    result = _replace(paths)

    assert result.internal_replaced is False
    assert paths[1].read_text(encoding="utf-8") == "new executable"
    assert destination_internal.lstat().st_ino == original_lstat.st_ino
    if destination_kind == "directory":
        assert (destination_internal / "old.txt").read_text(encoding="utf-8") == "old internal"
    elif destination_kind == "file":
        assert destination_internal.read_text(encoding="utf-8") == "old internal file"
    elif destination_kind == "symlink":
        assert (external / "old.txt").read_text(encoding="utf-8") == "external old"
    else:
        assert os.path.lexists(destination_internal) and not destination_internal.exists()


def test_missing_internal_preserves_destination_junction(tmp_path):
    paths = _bundle_paths(tmp_path)
    external = tmp_path / "external-internal"
    _write(external / "old.txt", "external old")
    _make_junction(paths[3], external)

    result = _replace(paths)

    assert result.internal_replaced is False
    assert paths[3].is_junction()
    assert (external / "old.txt").read_text(encoding="utf-8") == "external old"


def test_present_internal_installs_complete_tree_and_reports_replacement(tmp_path):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "runtime.dll", "runtime")
    _write(paths[2] / "nested" / "library.dat", "library")

    result = _replace(paths)

    assert result.internal_replaced is True
    assert paths[1].read_text(encoding="utf-8") == "new executable"
    assert (paths[3] / "runtime.dll").read_text(encoding="utf-8") == "runtime"
    assert (paths[3] / "nested" / "library.dat").read_text(encoding="utf-8") == "library"
    _assert_no_transaction_artifacts(paths[1].parent)


def test_present_internal_fully_replaces_existing_directory(tmp_path):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "current.txt", "current")
    _write(paths[3] / "current.txt", "outdated")
    _write(paths[3] / "stale.txt", "stale")

    result = _replace(paths)

    assert result.internal_replaced is True
    assert (paths[3] / "current.txt").read_text(encoding="utf-8") == "current"
    assert not (paths[3] / "stale.txt").exists()


@pytest.mark.parametrize("destination_kind", ["file", "symlink", "broken_symlink"])
def test_present_internal_replaces_non_directory_destination_as_leaf(tmp_path, destination_kind):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "current.txt", "current")
    external = tmp_path / "external-internal"
    if destination_kind == "file":
        _write(paths[3], "old file")
    elif destination_kind == "symlink":
        _write(external / "keep.txt", "keep")
        _make_symlink(paths[3], external, directory=True)
    else:
        _make_symlink(paths[3], tmp_path / "missing-external", directory=True)

    result = _replace(paths)

    assert result.internal_replaced is True
    assert paths[3].is_dir() and not paths[3].is_symlink()
    assert (paths[3] / "current.txt").read_text(encoding="utf-8") == "current"
    if destination_kind == "symlink":
        assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_present_internal_replaces_destination_junction_without_traversing_referent(tmp_path):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "current.txt", "current")
    external = tmp_path / "external-internal"
    _write(external / "keep.txt", "keep")
    _make_junction(paths[3], external)

    result = _replace(paths)

    assert result.internal_replaced is True
    assert paths[3].is_dir() and not paths[3].is_junction()
    assert (paths[3] / "current.txt").read_text(encoding="utf-8") == "current"
    assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("destination_kind", ["file", "directory", "symlink", "broken_symlink"])
def test_existing_executable_destination_object_is_replaced_as_a_leaf(tmp_path, destination_kind):
    paths = _bundle_paths(tmp_path)
    external = tmp_path / "external-executable"
    if destination_kind == "file":
        _write(paths[1], "old executable")
    elif destination_kind == "directory":
        _write(paths[1] / "keep.txt", "old directory")
    elif destination_kind == "symlink":
        _write(external, "external")
        _make_symlink(paths[1], external, directory=False)
    else:
        _make_symlink(paths[1], external, directory=False)

    result = _replace(paths)

    assert result.internal_replaced is False
    assert paths[1].is_file() and not paths[1].is_symlink()
    assert paths[1].read_text(encoding="utf-8") == "new executable"
    if destination_kind == "symlink":
        assert external.read_text(encoding="utf-8") == "external"


def test_existing_executable_destination_junction_is_replaced_as_a_leaf(tmp_path):
    paths = _bundle_paths(tmp_path)
    external = tmp_path / "external-executable"
    _write(external / "keep.txt", "keep")
    _make_junction(paths[1], external)

    result = _replace(paths)

    assert result.internal_replaced is False
    assert paths[1].is_file() and not paths[1].is_junction()
    assert paths[1].read_text(encoding="utf-8") == "new executable"
    assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("source_kind", ["missing", "directory", "symlink", "broken_symlink"])
def test_invalid_executable_source_fails_before_destination_mutation(tmp_path, source_kind):
    paths = list(_bundle_paths(tmp_path))
    paths[0].unlink()
    if source_kind == "directory":
        paths[0].mkdir()
    elif source_kind == "symlink":
        external = _write(tmp_path / "external.exe", "external")
        _make_symlink(paths[0], external, directory=False)
    elif source_kind == "broken_symlink":
        _make_symlink(paths[0], tmp_path / "missing.exe", directory=False)
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")

    with pytest.raises(ProgramBundleError):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert (paths[3] / "old.txt").read_text(encoding="utf-8") == "old internal"


def test_executable_source_junction_fails_before_destination_mutation(tmp_path):
    paths = list(_bundle_paths(tmp_path))
    paths[0].unlink()
    external = tmp_path / "external-executable"
    _write(external / "payload", "external")
    _make_junction(paths[0], external)
    _write(paths[1], "old executable")

    with pytest.raises(ProgramBundleError):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert (external / "payload").read_text(encoding="utf-8") == "external"


def test_unreadable_executable_source_fails_before_destination_mutation(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[1], "old executable")

    def deny_open(_path):
        raise PermissionError("denied")

    monkeypatch.setattr(program_bundle, "_open_source_descriptor", deny_open)

    with pytest.raises(ProgramBundleError, match="denied"):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"


def test_non_regular_executable_source_fails_before_destination_mutation(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[1], "old executable")
    real_stat = program_bundle._stat_no_follow

    def fake_stat(path):
        result = real_stat(path)
        if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(paths[0])):
            values = list(result)
            values[stat.ST_MODE] = stat.S_IFIFO | 0o600
            return os.stat_result(values)
        return result

    monkeypatch.setattr(program_bundle, "_stat_no_follow", fake_stat)

    with pytest.raises(ProgramBundleError, match="regular file"):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"


@pytest.mark.parametrize("source_kind", ["file", "symlink", "broken_symlink"])
def test_invalid_internal_source_fails_before_destination_mutation(tmp_path, source_kind):
    paths = _bundle_paths(tmp_path)
    if source_kind == "file":
        _write(paths[2], "not a directory")
    elif source_kind == "symlink":
        external = tmp_path / "external-internal"
        external.mkdir()
        _make_symlink(paths[2], external, directory=True)
    else:
        _make_symlink(paths[2], tmp_path / "missing-internal", directory=True)
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")

    with pytest.raises(ProgramBundleError):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert (paths[3] / "old.txt").read_text(encoding="utf-8") == "old internal"


def test_internal_source_junction_fails_before_destination_mutation(tmp_path):
    paths = _bundle_paths(tmp_path)
    external = tmp_path / "external-internal"
    _write(external / "payload", "external")
    _make_junction(paths[2], external)
    _write(paths[1], "old executable")

    with pytest.raises(ProgramBundleError):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert (external / "payload").read_text(encoding="utf-8") == "external"


@pytest.mark.parametrize("nested_kind", ["file_symlink", "directory_symlink"])
def test_nested_source_symlink_fails_before_destination_mutation(tmp_path, nested_kind):
    paths = _bundle_paths(tmp_path)
    paths[2].mkdir()
    external = tmp_path / "external"
    if nested_kind == "file_symlink":
        _write(external, "external")
        _make_symlink(paths[2] / "linked", external, directory=False)
    else:
        external.mkdir()
        _make_symlink(paths[2] / "linked", external, directory=True)
    _write(paths[1], "old executable")

    with pytest.raises(ProgramBundleError):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"


def test_nested_source_junction_fails_before_destination_mutation(tmp_path):
    paths = _bundle_paths(tmp_path)
    paths[2].mkdir()
    external = tmp_path / "external"
    _write(external / "payload", "external")
    _make_junction(paths[2] / "linked", external)
    _write(paths[1], "old executable")

    with pytest.raises(ProgramBundleError):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert (external / "payload").read_text(encoding="utf-8") == "external"


def test_nested_non_regular_source_entry_fails_before_destination_mutation(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    special = _write(paths[2] / "special", "placeholder")
    _write(paths[1], "old executable")
    real_stat = program_bundle._stat_no_follow

    def fake_stat(path):
        result = real_stat(path)
        if os.path.normcase(os.fspath(path)) == os.path.normcase(os.fspath(special)):
            values = list(result)
            values[stat.ST_MODE] = stat.S_IFIFO | 0o600
            return os.stat_result(values)
        return result

    monkeypatch.setattr(program_bundle, "_stat_no_follow", fake_stat)

    with pytest.raises(ProgramBundleError, match="unsupported source entry"):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"


def test_source_file_substituted_with_link_before_descriptor_recheck_is_rejected(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    source_file = _write(paths[2] / "runtime.dll", "trusted")
    external = _write(tmp_path / "external.dll", "untrusted")
    _write(paths[1], "old executable")
    original_copy = program_bundle._copy_regular_file
    changed = False

    def substitute_then_copy(source, destination, expected_stat=None):
        nonlocal changed
        if Path(source) == source_file and not changed:
            changed = True
            source_file.unlink()
            _make_symlink(source_file, external, directory=False)
        return original_copy(source, destination, expected_stat=expected_stat)

    monkeypatch.setattr(program_bundle, "_copy_regular_file", substitute_then_copy)

    with pytest.raises(ProgramBundleError):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert not paths[3].exists()


def test_internal_source_substituted_with_junction_before_staging_recheck_is_rejected(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "runtime.dll", "trusted")
    external = tmp_path / "external-internal"
    _write(external / "runtime.dll", "untrusted")
    _write(paths[1], "old executable")
    original_stage = program_bundle._stage_internal_directory
    changed = False

    def substitute_then_stage(source, destination, expected_stat):
        nonlocal changed
        if not changed:
            changed = True
            shutil.rmtree(source)
            _make_junction(source, external)
        return original_stage(source, destination, expected_stat)

    monkeypatch.setattr(program_bundle, "_stage_internal_directory", substitute_then_stage)

    with pytest.raises(ProgramBundleError):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert not paths[3].exists()
    assert (external / "runtime.dll").read_text(encoding="utf-8") == "untrusted"


def test_rejects_destination_paths_with_different_resolved_parents(tmp_path):
    paths = list(_bundle_paths(tmp_path))
    paths[3] = tmp_path / "other" / "_internal"

    with pytest.raises(ProgramBundleError, match="same resolved parent"):
        _replace(paths)


@pytest.mark.parametrize(
    ("index", "leaf"),
    [(1, "Other.exe"), (3, "other-internal")],
)
def test_rejects_unexpected_destination_leaf_names(tmp_path, index, leaf):
    paths = list(_bundle_paths(tmp_path))
    paths[index] = paths[index].with_name(leaf)

    with pytest.raises(ProgramBundleError, match="destination leaf"):
        _replace(paths)


def _transaction_artifacts(destination_parent, kind=None):
    paths = [path for path in destination_parent.iterdir() if ".program-bundle-" in path.name]
    if kind is not None:
        paths = [path for path in paths if f"program-bundle-{kind}-" in path.name]
    return sorted(paths, key=lambda path: path.name)


@pytest.mark.parametrize("failure_boundary", ["internal_displacement", "executable_displacement", "internal_install", "executable_install"])
def test_commit_failure_restores_both_original_destinations(tmp_path, monkeypatch, failure_boundary):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    real_replace = program_bundle.os.replace
    commit_call = 0
    boundaries = {
        1: "internal_displacement",
        2: "executable_displacement",
        3: "internal_install",
        4: "executable_install",
    }

    def fail_at_boundary(source, destination):
        nonlocal commit_call
        commit_call += 1
        if boundaries.get(commit_call) == failure_boundary:
            raise OSError(f"failed {failure_boundary}")
        return real_replace(source, destination)

    monkeypatch.setattr(program_bundle.os, "replace", fail_at_boundary)

    with pytest.raises(ProgramBundleError, match=failure_boundary):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert (paths[3] / "old.txt").read_text(encoding="utf-8") == "old internal"
    assert not (paths[3] / "new.txt").exists()
    _assert_no_transaction_artifacts(paths[1].parent)


@pytest.mark.parametrize("staging_boundary", ["executable", "internal"])
def test_staging_failure_removes_partial_artifacts_and_preserves_destinations(tmp_path, monkeypatch, staging_boundary):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    if staging_boundary == "executable":
        real_copy = program_bundle._copy_regular_file

        def fail_executable_copy(source, destination, expected_stat=None):
            Path(destination).write_text("partial", encoding="utf-8")
            if Path(source) == paths[0]:
                raise OSError("failed executable staging")
            return real_copy(source, destination, expected_stat=expected_stat)

        monkeypatch.setattr(program_bundle, "_copy_regular_file", fail_executable_copy)
    else:
        def fail_internal_copy(_source, destination, expected_stat=None):
            _write(Path(destination) / "partial.txt", "partial")
            raise OSError("failed internal staging")

        monkeypatch.setattr(program_bundle, "_stage_internal_directory", fail_internal_copy)

    with pytest.raises(ProgramBundleError, match=f"failed {staging_boundary} staging"):
        _replace(paths)

    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert (paths[3] / "old.txt").read_text(encoding="utf-8") == "old internal"
    _assert_no_transaction_artifacts(paths[1].parent)


def test_failed_staging_cleanup_reports_and_retains_exact_artifact(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    real_remove = program_bundle._remove_leaf

    def fail_copy(_source, destination, expected_stat=None):
        Path(destination).write_text("partial", encoding="utf-8")
        raise OSError("copy failed")

    def fail_stage_cleanup(path):
        if "program-bundle-stage" in Path(path).name:
            raise OSError("cleanup denied")
        return real_remove(path)

    monkeypatch.setattr(program_bundle, "_copy_regular_file", fail_copy)
    monkeypatch.setattr(program_bundle, "_remove_leaf", fail_stage_cleanup)

    with pytest.raises(ProgramBundleError) as raised:
        _replace(paths)

    retained = _transaction_artifacts(paths[1].parent, "stage")
    assert len(retained) == 1
    assert str(retained[0]) in str(raised.value)
    assert "retained staging artifact" in str(raised.value)


@pytest.mark.parametrize("alias_kind", ["symlink", "junction"])
def test_staging_alias_cleanup_is_leaf_only_and_preserves_external_referent(
    tmp_path, monkeypatch, alias_kind
):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    external = tmp_path / "external-stage"
    _write(external / "keep.txt", "keep")

    def substitute_staging_with_alias(_source, destination, expected_stat=None):
        shutil.rmtree(destination)
        if alias_kind == "junction":
            _make_junction(Path(destination), external)
        else:
            _make_symlink(Path(destination), external, directory=True)
        raise OSError("staging failed after substitution")

    monkeypatch.setattr(program_bundle, "_stage_internal_directory", substitute_staging_with_alias)

    with pytest.raises(ProgramBundleError, match="staging failed after substitution"):
        _replace(paths)

    assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"
    _assert_no_transaction_artifacts(paths[1].parent)


def test_rollback_failure_reports_recovery_and_never_deletes_only_prior_object(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    real_replace = program_bundle.os.replace
    call = 0

    def fail_install_and_internal_restore(source, destination):
        nonlocal call
        call += 1
        if call == 4:
            raise OSError("install failed")
        if "program-bundle-recovery" in Path(source).name and Path(destination) == paths[3]:
            raise OSError("restore failed")
        return real_replace(source, destination)

    monkeypatch.setattr(program_bundle.os, "replace", fail_install_and_internal_restore)

    with pytest.raises(ProgramBundleError) as raised:
        _replace(paths)

    retained = _transaction_artifacts(paths[1].parent, "recovery")
    assert len(retained) == 1
    assert str(retained[0]) in str(raised.value)
    assert retained[0].is_dir()
    assert (retained[0] / "old.txt").read_text(encoding="utf-8") == "old internal"
    assert paths[1].read_text(encoding="utf-8") == "old executable"


def test_removal_failure_keeps_recovery_available_and_reports_it(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    real_replace = program_bundle.os.replace
    real_remove = program_bundle._remove_leaf
    replace_call = 0

    def fail_executable_install(source, destination):
        nonlocal replace_call
        replace_call += 1
        if replace_call == 4:
            raise OSError("install failed")
        return real_replace(source, destination)

    def fail_installed_internal_removal(path):
        if Path(path) == paths[3]:
            raise OSError("cannot remove installed internal")
        return real_remove(path)

    monkeypatch.setattr(program_bundle.os, "replace", fail_executable_install)
    monkeypatch.setattr(program_bundle, "_remove_leaf", fail_installed_internal_removal)

    with pytest.raises(ProgramBundleError) as raised:
        _replace(paths)

    recoveries = _transaction_artifacts(paths[1].parent, "recovery")
    assert len(recoveries) == 1
    assert str(recoveries[0]) in str(raised.value)
    assert (recoveries[0] / "old.txt").read_text(encoding="utf-8") == "old internal"


@pytest.mark.parametrize("destination_kind", ["directory", "file", "symlink", "broken_symlink"])
@pytest.mark.parametrize("failure_boundary", ["staging", "displacement", "installation"])
def test_absent_source_internal_is_untouched_through_executable_failures(
    tmp_path, monkeypatch, destination_kind, failure_boundary
):
    paths = _bundle_paths(tmp_path)
    external = tmp_path / "external-internal"
    if destination_kind == "directory":
        _write(paths[3] / "old.txt", "old internal")
    elif destination_kind == "file":
        _write(paths[3], "old internal")
    elif destination_kind == "symlink":
        _write(external / "old.txt", "external old")
        _make_symlink(paths[3], external, directory=True)
    else:
        _make_symlink(paths[3], tmp_path / "missing-external", directory=True)
    original = paths[3].lstat()
    _write(paths[1], "old executable")
    if failure_boundary == "staging":
        monkeypatch.setattr(
            program_bundle,
            "_copy_regular_file",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("staging failure")),
        )
    else:
        real_replace = program_bundle.os.replace
        call = 0

        def fail_replace(source, destination):
            nonlocal call
            call += 1
            if call == (1 if failure_boundary == "displacement" else 2):
                raise OSError(f"{failure_boundary} failure")
            return real_replace(source, destination)

        monkeypatch.setattr(program_bundle.os, "replace", fail_replace)

    with pytest.raises(ProgramBundleError):
        _replace(paths)

    assert paths[3].lstat().st_ino == original.st_ino
    if destination_kind == "symlink":
        assert (external / "old.txt").read_text(encoding="utf-8") == "external old"
    elif destination_kind == "broken_symlink":
        assert os.path.lexists(paths[3]) and not paths[3].exists()


@pytest.mark.parametrize("alias_kind", ["symlink", "broken_symlink"])
@pytest.mark.parametrize("cleanup_context", ["success", "rollback"])
def test_recovery_alias_cleanup_is_leaf_only_and_preserves_external_referent(
    tmp_path, monkeypatch, alias_kind, cleanup_context
):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    external = tmp_path / "external-old"
    if alias_kind == "symlink":
        _write(external / "keep.txt", "keep")
        _make_symlink(paths[3], external, directory=True)
    else:
        _make_symlink(paths[3], tmp_path / "missing-external", directory=True)
    if cleanup_context == "rollback":
        real_replace = program_bundle.os.replace
        call = 0

        def fail_executable_install(source, destination):
            nonlocal call
            call += 1
            if call == 4:
                raise OSError("install failed")
            return real_replace(source, destination)

        monkeypatch.setattr(program_bundle.os, "replace", fail_executable_install)
        with pytest.raises(ProgramBundleError):
            _replace(paths)
        assert os.path.lexists(paths[3])
    else:
        _replace(paths)
        assert paths[3].is_dir() and not paths[3].is_symlink()
    if alias_kind == "symlink":
        assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"
    _assert_no_transaction_artifacts(paths[1].parent)


def test_recovery_junction_cleanup_is_leaf_only_and_preserves_external_referent(tmp_path):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    external = tmp_path / "external-old"
    _write(external / "keep.txt", "keep")
    _make_junction(paths[3], external)

    _replace(paths)

    assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"
    _assert_no_transaction_artifacts(paths[1].parent)


def test_absent_source_internal_junction_is_untouched_during_executable_rollback(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    external = tmp_path / "external-internal"
    _write(external / "keep.txt", "keep")
    _make_junction(paths[3], external)
    _write(paths[1], "old executable")
    real_replace = program_bundle.os.replace
    call = 0

    def fail_executable_install(source, destination):
        nonlocal call
        call += 1
        if call == 2:
            raise OSError("installation failed")
        return real_replace(source, destination)

    monkeypatch.setattr(program_bundle.os, "replace", fail_executable_install)

    with pytest.raises(ProgramBundleError, match="installation failed"):
        _replace(paths)

    assert paths[3].is_junction()
    assert (external / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert paths[1].read_text(encoding="utf-8") == "old executable"


@pytest.mark.parametrize("signal", [KeyboardInterrupt("stop"), SystemExit(17)], ids=["interrupt", "system_exit"])
@pytest.mark.parametrize("boundary", ["staging", "displacement", "installation"])
def test_control_flow_signal_rolls_back_and_cleans_before_propagating(tmp_path, monkeypatch, signal, boundary):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    if boundary == "staging":
        def signal_during_stage(source, destination, expected_stat):
            _write(Path(destination) / "partial.txt", "partial")
            raise signal

        monkeypatch.setattr(program_bundle, "_stage_internal_directory", signal_during_stage)
    else:
        real_replace = program_bundle.os.replace
        call = 0

        def signal_during_replace(source, destination):
            nonlocal call
            call += 1
            target_call = 2 if boundary == "displacement" else 4
            if call == target_call:
                raise signal
            return real_replace(source, destination)

        monkeypatch.setattr(program_bundle.os, "replace", signal_during_replace)

    with pytest.raises(type(signal)) as raised:
        _replace(paths)

    assert raised.value is signal
    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert (paths[3] / "old.txt").read_text(encoding="utf-8") == "old internal"
    _assert_no_transaction_artifacts(paths[1].parent)


def test_first_control_flow_signal_wins_and_remaining_recovery_is_attempted(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    first = KeyboardInterrupt("first")
    real_replace = program_bundle.os.replace
    real_remove = program_bundle._remove_leaf
    replace_call = 0
    removed = []

    def signal_during_install(source, destination):
        nonlocal replace_call
        replace_call += 1
        if replace_call == 4:
            raise first
        return real_replace(source, destination)

    def secondary_signal_during_cleanup(path):
        removed.append(Path(path).name)
        if Path(path) == paths[3]:
            raise SystemExit(99)
        return real_remove(path)

    monkeypatch.setattr(program_bundle.os, "replace", signal_during_install)
    monkeypatch.setattr(program_bundle, "_remove_leaf", secondary_signal_during_cleanup)

    with pytest.raises(KeyboardInterrupt) as raised:
        _replace(paths)

    assert raised.value is first
    assert any("program-bundle-stage" in name for name in removed)
    assert paths[1].read_text(encoding="utf-8") == "old executable"


def test_control_flow_during_rollback_after_ordinary_failure_propagates_and_recovery_continues(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    first = KeyboardInterrupt("rollback stop")
    real_replace = program_bundle.os.replace
    call = 0

    def fail_install_then_signal_restore(source, destination):
        nonlocal call
        call += 1
        if call == 4:
            raise OSError("ordinary install failure")
        if "program-bundle-recovery" in Path(source).name and Path(destination) == paths[1]:
            raise first
        return real_replace(source, destination)

    monkeypatch.setattr(program_bundle.os, "replace", fail_install_then_signal_restore)

    with pytest.raises(KeyboardInterrupt) as raised:
        _replace(paths)

    assert raised.value is first
    assert (paths[3] / "old.txt").read_text(encoding="utf-8") == "old internal"
    assert _transaction_artifacts(paths[1].parent, "recovery")


def test_control_flow_during_staging_cleanup_after_ordinary_failure_attempts_all_artifacts(
    tmp_path, monkeypatch
):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    first = KeyboardInterrupt("cleanup stop")
    real_remove = program_bundle._remove_leaf
    attempts = []

    def fail_internal_stage(_source, destination, expected_stat=None):
        _write(Path(destination) / "partial.txt", "partial")
        raise OSError("ordinary staging failure")

    def signal_on_first_cleanup(path):
        attempts.append(Path(path).name)
        if len(attempts) == 1:
            raise first
        return real_remove(path)

    monkeypatch.setattr(program_bundle, "_stage_internal_directory", fail_internal_stage)
    monkeypatch.setattr(program_bundle, "_remove_leaf", signal_on_first_cleanup)

    with pytest.raises(KeyboardInterrupt) as raised:
        _replace(paths)

    assert raised.value is first
    assert len(attempts) == 2
    retained = _transaction_artifacts(paths[1].parent, "stage")
    assert len(retained) == 1


def test_post_commit_cleanup_control_flow_keeps_first_signal_and_attempts_all_artifacts(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    first = KeyboardInterrupt("cleanup stop")
    real_remove = program_bundle._remove_leaf
    attempts = []

    def signal_on_first_recovery(path):
        attempts.append(Path(path).name)
        if len(attempts) == 1:
            raise first
        return real_remove(path)

    monkeypatch.setattr(program_bundle, "_remove_leaf", signal_on_first_recovery)

    with pytest.raises(KeyboardInterrupt) as raised:
        _replace(paths)

    assert raised.value is first
    assert len(attempts) == 2
    assert paths[1].read_text(encoding="utf-8") == "new executable"
    assert (paths[3] / "new.txt").read_text(encoding="utf-8") == "new internal"


def test_warning_reporter_control_flow_is_not_swallowed_and_other_cleanup_continues(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    reporter_signal = SystemExit(23)
    real_remove = program_bundle._remove_leaf
    cleanup_attempts = 0

    def fail_first_cleanup(path):
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise OSError("cleanup failed")
        return real_remove(path)

    def reporter(_message):
        raise reporter_signal

    monkeypatch.setattr(program_bundle, "_remove_leaf", fail_first_cleanup)

    with pytest.raises(SystemExit) as raised:
        _replace(paths, warning_reporter=reporter)

    assert raised.value is reporter_signal
    assert cleanup_attempts == 2


def test_post_commit_cleanup_failure_warns_with_exact_retained_recovery_path(tmp_path, monkeypatch):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    real_remove = program_bundle._remove_leaf
    cleanup_attempts = 0
    warnings = []

    def fail_first_cleanup(path):
        nonlocal cleanup_attempts
        cleanup_attempts += 1
        if cleanup_attempts == 1:
            raise OSError("cleanup failed")
        return real_remove(path)

    monkeypatch.setattr(program_bundle, "_remove_leaf", fail_first_cleanup)

    result = _replace(paths, warning_reporter=warnings.append)

    retained = _transaction_artifacts(paths[1].parent, "recovery")
    assert result.internal_replaced is True
    assert cleanup_attempts == 2
    assert len(retained) == 1
    assert warnings == [
        f"failed to clean retained recovery artifact {retained[0]}: cleanup failed"
    ]


def test_rollback_existence_probe_control_flow_is_preserved_and_later_cleanup_runs(
    tmp_path, monkeypatch
):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    first = SystemExit(31)
    real_exists = program_bundle._artifact_exists
    real_remove = program_bundle._remove_leaf
    existence_calls = 0
    removal_attempts = []

    def fail_first_existence_probe(path):
        nonlocal existence_calls
        if path is None:
            return real_exists(path)
        existence_calls += 1
        if existence_calls == 1:
            raise first
        return real_exists(path)

    def record_removal(path):
        removal_attempts.append(Path(path).name)
        return real_remove(path)

    def fail_internal_stage(_source, destination, expected_stat=None):
        _write(Path(destination) / "partial.txt", "partial")
        raise OSError("ordinary staging failure")

    monkeypatch.setattr(program_bundle, "_artifact_exists", fail_first_existence_probe)
    monkeypatch.setattr(program_bundle, "_remove_leaf", record_removal)
    monkeypatch.setattr(program_bundle, "_stage_internal_directory", fail_internal_stage)

    with pytest.raises(SystemExit) as raised:
        _replace(paths)

    assert raised.value is first
    assert len(removal_attempts) == 1
    assert removal_attempts[0].startswith("._internal.program-bundle-stage-")


def test_rollback_secondary_existence_signal_does_not_replace_first_or_stop_cleanup(
    tmp_path, monkeypatch
):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    first = KeyboardInterrupt("first removal signal")
    secondary = SystemExit(32)
    real_exists = program_bundle._artifact_exists
    real_remove = program_bundle._remove_leaf
    existence_calls = 0
    removal_attempts = []

    def signal_during_recheck(path):
        nonlocal existence_calls
        if path is None:
            return real_exists(path)
        existence_calls += 1
        if existence_calls == 2:
            raise secondary
        return real_exists(path)

    def signal_then_remove(path):
        removal_attempts.append(Path(path).name)
        if len(removal_attempts) == 1:
            raise first
        return real_remove(path)

    def fail_internal_stage(_source, destination, expected_stat=None):
        _write(Path(destination) / "partial.txt", "partial")
        raise OSError("ordinary staging failure")

    monkeypatch.setattr(program_bundle, "_artifact_exists", signal_during_recheck)
    monkeypatch.setattr(program_bundle, "_remove_leaf", signal_then_remove)
    monkeypatch.setattr(program_bundle, "_stage_internal_directory", fail_internal_stage)

    with pytest.raises(KeyboardInterrupt) as raised:
        _replace(paths)

    assert raised.value is first
    assert len(removal_attempts) == 2
    assert removal_attempts[1].startswith("._internal.program-bundle-stage-")


def test_post_commit_existence_probe_control_flow_is_preserved_and_later_cleanup_runs(
    tmp_path, monkeypatch
):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    first = SystemExit(33)
    real_exists = program_bundle._artifact_exists
    real_remove = program_bundle._remove_leaf
    existence_calls = 0
    removal_attempts = []

    def fail_first_existence_probe(path):
        nonlocal existence_calls
        existence_calls += 1
        if existence_calls == 1:
            raise first
        return real_exists(path)

    def record_removal(path):
        removal_attempts.append(Path(path).name)
        return real_remove(path)

    monkeypatch.setattr(program_bundle, "_artifact_exists", fail_first_existence_probe)
    monkeypatch.setattr(program_bundle, "_remove_leaf", record_removal)

    with pytest.raises(SystemExit) as raised:
        _replace(paths)

    assert raised.value is first
    assert len(removal_attempts) == 1
    assert removal_attempts[0].startswith("._internal.program-bundle-recovery-")
    assert paths[1].read_text(encoding="utf-8") == "new executable"
    assert (paths[3] / "new.txt").read_text(encoding="utf-8") == "new internal"


def test_post_commit_secondary_existence_signal_does_not_replace_first_or_stop_cleanup(
    tmp_path, monkeypatch
):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    first = KeyboardInterrupt("first removal signal")
    secondary = SystemExit(34)
    real_exists = program_bundle._artifact_exists
    real_remove = program_bundle._remove_leaf
    existence_calls = 0
    removal_attempts = []

    def signal_during_recheck(path):
        nonlocal existence_calls
        existence_calls += 1
        if existence_calls == 2:
            raise secondary
        return real_exists(path)

    def signal_then_remove(path):
        removal_attempts.append(Path(path).name)
        if len(removal_attempts) == 1:
            raise first
        return real_remove(path)

    monkeypatch.setattr(program_bundle, "_artifact_exists", signal_during_recheck)
    monkeypatch.setattr(program_bundle, "_remove_leaf", signal_then_remove)

    with pytest.raises(KeyboardInterrupt) as raised:
        _replace(paths)

    assert raised.value is first
    assert len(removal_attempts) == 2
    assert removal_attempts[1].startswith("._internal.program-bundle-recovery-")


@pytest.mark.parametrize(
    ("boundary", "target_call"),
    [
        ("internal_displacement", 1),
        ("executable_displacement", 2),
        ("internal_installation", 3),
        ("executable_installation", 4),
    ],
)
@pytest.mark.parametrize(
    "signal",
    [KeyboardInterrupt("after effect"), SystemExit(41)],
    ids=["interrupt", "system_exit"],
)
def test_after_effect_control_flow_reconciles_transition_and_restores_original_bundle(
    tmp_path, monkeypatch, boundary, target_call, signal
):
    paths = _bundle_paths(tmp_path)
    _write(paths[2] / "new.txt", "new internal")
    _write(paths[1], "old executable")
    _write(paths[3] / "old.txt", "old internal")
    original_executable = paths[1].stat()
    original_internal = paths[3].stat()
    real_replace = program_bundle.os.replace
    replace_calls = 0

    def replace_then_signal(source, destination):
        nonlocal replace_calls
        replace_calls += 1
        result = real_replace(source, destination)
        if replace_calls == target_call:
            raise signal
        return result

    monkeypatch.setattr(program_bundle.os, "replace", replace_then_signal)

    with pytest.raises(type(signal)) as raised:
        _replace(paths)

    assert raised.value is signal, boundary
    assert paths[1].stat().st_ino == original_executable.st_ino
    assert paths[1].read_text(encoding="utf-8") == "old executable"
    assert paths[3].stat().st_ino == original_internal.st_ino
    assert (paths[3] / "old.txt").read_text(encoding="utf-8") == "old internal"
    assert not (paths[3] / "new.txt").exists()
    _assert_no_transaction_artifacts(paths[1].parent)
