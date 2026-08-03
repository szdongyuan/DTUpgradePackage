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
