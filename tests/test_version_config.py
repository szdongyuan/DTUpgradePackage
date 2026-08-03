from pathlib import Path

import pytest

from consts.version_config import (
    LEGACY_INITIAL_VERSION,
    UnknownVersionError,
    VersionConfigError,
    compare_version_positions,
    load_version_config,
    resolve_application_dir,
    select_upgrade_versions,
)


def write_version_config(path, content):
    path.write_text(content, encoding="utf-8")
    return path


def assert_config_error(path, reason, action):
    with pytest.raises(VersionConfigError) as exc_info:
        action()

    message = str(exc_info.value)
    assert str(path) in message
    assert reason in message


def test_load_version_config_preserves_declared_order(tmp_path):
    path = write_version_config(
        tmp_path / "version.yml",
        'versions: ["0.12", "10", "2"]\ntarget_version: "2"\n',
    )

    versions, target = load_version_config(path)

    assert versions == ["0.12", "10", "2"]
    assert target == "2"


def test_load_version_config_rejects_missing_file(tmp_path):
    path = tmp_path / "missing.yml"

    assert_config_error(path, "unable to read", lambda: load_version_config(path))


def test_load_version_config_wraps_invalid_utf8(tmp_path):
    path = tmp_path / "version.yml"
    path.write_bytes(b"versions: [\xff]\n")

    with pytest.raises(VersionConfigError) as exc_info:
        load_version_config(path)

    assert str(path) in str(exc_info.value)
    assert "UTF-8" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)


def test_load_version_config_wraps_unreadable_file(tmp_path, monkeypatch):
    path = write_version_config(
        tmp_path / "version.yml",
        'versions: ["0.12"]\ntarget_version: "0.12"\n',
    )

    def deny_read(self, *args, **kwargs):
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "read_text", deny_read)

    with pytest.raises(VersionConfigError) as exc_info:
        load_version_config(path)

    assert str(path) in str(exc_info.value)
    assert "unable to read" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, PermissionError)


def test_load_version_config_wraps_malformed_yaml(tmp_path):
    path = write_version_config(tmp_path / "version.yml", "versions: [unterminated\n")

    assert_config_error(path, "invalid YAML", lambda: load_version_config(path))


@pytest.mark.parametrize("document", ["scalar\n", "- 0.12\n"])
def test_load_version_config_requires_mapping_document(tmp_path, document):
    path = write_version_config(tmp_path / "version.yml", document)

    assert_config_error(path, "mapping", lambda: load_version_config(path))


@pytest.mark.parametrize(
    ("document", "reason"),
    [
        ('target_version: "0.12"\n', "versions must be a non-empty list"),
        ('versions: []\ntarget_version: "0.12"\n', "versions must be a non-empty list"),
        ('versions: "0.12"\ntarget_version: "0.12"\n', "versions must be a non-empty list"),
    ],
)
def test_load_version_config_requires_non_empty_versions_list(tmp_path, document, reason):
    path = write_version_config(tmp_path / "version.yml", document)

    assert_config_error(path, reason, lambda: load_version_config(path))


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("123", "version entry 1 must be a non-empty string"),
        ('""', "version entry 1 must be a non-empty string"),
        ('" 1"', "version entry 1 is not a safe identifier"),
        ('"1 "', "version entry 1 is not a safe identifier"),
        ('"."', "version entry 1 is not a safe identifier"),
        ('".."', "version entry 1 is not a safe identifier"),
        ('"1/2"', "version entry 1 is not a safe identifier"),
        ('"1\\\\2"', "version entry 1 is not a safe identifier"),
        ('"1\\0x"', "version entry 1 is not a safe identifier"),
        ('"1:2"', "version entry 1 is not a safe identifier"),
    ],
)
def test_load_version_config_rejects_invalid_version_entries(tmp_path, entry, reason):
    path = write_version_config(
        tmp_path / "version.yml",
        f'versions: ["{LEGACY_INITIAL_VERSION}", {entry}]\n'
        f'target_version: "{LEGACY_INITIAL_VERSION}"\n',
    )

    assert_config_error(path, reason, lambda: load_version_config(path))


def test_load_version_config_rejects_duplicate_versions(tmp_path):
    path = write_version_config(
        tmp_path / "version.yml",
        'versions: ["0.12", "1", "1"]\ntarget_version: "1"\n',
    )

    assert_config_error(path, "versions must be unique", lambda: load_version_config(path))


def test_load_version_config_requires_legacy_initial_version(tmp_path):
    path = write_version_config(
        tmp_path / "version.yml",
        'versions: ["1", "2"]\ntarget_version: "2"\n',
    )

    assert_config_error(
        path,
        f"versions must contain {LEGACY_INITIAL_VERSION}",
        lambda: load_version_config(path),
    )


@pytest.mark.parametrize(
    ("target_line", "reason"),
    [
        ("", "target_version must be a non-empty string"),
        ("target_version: 123\n", "target_version must be a non-empty string"),
        ('target_version: ""\n', "target_version must be a non-empty string"),
        ('target_version: " 0.12"\n', "target_version is not a safe identifier"),
        ('target_version: "0.12 "\n', "target_version is not a safe identifier"),
        ('target_version: "."\n', "target_version is not a safe identifier"),
        ('target_version: ".."\n', "target_version is not a safe identifier"),
        ('target_version: "0/12"\n', "target_version is not a safe identifier"),
        ('target_version: "0\\\\12"\n', "target_version is not a safe identifier"),
        ('target_version: "0\\0x"\n', "target_version is not a safe identifier"),
        ('target_version: "2"\n', "target_version must occur in versions"),
    ],
)
def test_load_version_config_rejects_invalid_target(tmp_path, target_line, reason):
    path = write_version_config(
        tmp_path / "version.yml",
        'versions: ["0.12", "1"]\n' + target_line,
    )

    assert_config_error(path, reason, lambda: load_version_config(path))


def test_resolve_application_dir_uses_source_module_repository_root(tmp_path):
    repo = tmp_path / "repo"

    assert resolve_application_dir(
        module_file=repo / "consts" / "program_config.py",
        executable=tmp_path / "python.exe",
        frozen=False,
    ) == repo


def test_resolve_application_dir_uses_frozen_executable_parent(tmp_path):
    assert resolve_application_dir(
        module_file=tmp_path / "_MEI" / "consts" / "program_config.py",
        executable=tmp_path / "package" / "main.exe",
        frozen=True,
    ) == tmp_path / "package"


def test_select_upgrade_versions_uses_declared_order_and_target_boundary():
    versions = ["0.12", "10", "2", "3"]

    assert select_upgrade_versions(versions, "2", "0.12") == ["10", "2"]
    assert select_upgrade_versions(versions, "2", "10") == ["2"]
    assert select_upgrade_versions(versions, "2", "2") == []
    assert select_upgrade_versions(versions, "2", "3") == []


def test_compare_version_positions_uses_declared_indices():
    versions = ["0.12", "10", "2", "3"]

    assert compare_version_positions(versions, "2", "10") == -1
    assert compare_version_positions(versions, "2", "2") == 0
    assert compare_version_positions(versions, "2", "3") == 1


@pytest.mark.parametrize("helper", [compare_version_positions, select_upgrade_versions])
@pytest.mark.parametrize(
    ("target", "current", "unknown"),
    [("2", "unknown", "current version"), ("unknown", "0.12", "target version")],
)
def test_sequence_helpers_reject_unknown_versions(helper, target, current, unknown):
    with pytest.raises(UnknownVersionError, match=unknown):
        helper(["0.12", "10", "2", "3"], target, current)
