import re
from pathlib import Path

import yaml


LEGACY_INITIAL_VERSION = "0.12"

_VERSION_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class VersionConfigError(ValueError):
    pass


class UnknownVersionError(VersionConfigError):
    pass


def resolve_application_dir(module_file, executable, frozen):
    if frozen:
        return Path(executable).resolve().parent
    return Path(module_file).resolve().parent.parent


def _is_safe_identifier(value):
    return value not in {".", ".."} and _VERSION_IDENTIFIER.fullmatch(value) is not None


def _config_error(path, reason):
    return VersionConfigError(f"Invalid version configuration at {path}: {reason}")


def load_version_config(path):
    path = Path(path)
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _config_error(path, "file is not valid UTF-8") from exc
    except OSError as exc:
        raise _config_error(path, f"unable to read file: {exc}") from exc

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise _config_error(path, f"invalid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise _config_error(path, "document must be a mapping")

    versions = data.get("versions")
    if not isinstance(versions, list) or not versions:
        raise _config_error(path, "versions must be a non-empty list")

    for index, version in enumerate(versions):
        if not isinstance(version, str) or not version:
            raise _config_error(path, f"version entry {index} must be a non-empty string")
        if not _is_safe_identifier(version):
            raise _config_error(path, f"version entry {index} is not a safe identifier")

    if len(versions) != len(set(versions)):
        raise _config_error(path, "versions must be unique")
    if LEGACY_INITIAL_VERSION not in versions:
        raise _config_error(path, f"versions must contain {LEGACY_INITIAL_VERSION}")

    target_version = data.get("target_version")
    if not isinstance(target_version, str) or not target_version:
        raise _config_error(path, "target_version must be a non-empty string")
    if not _is_safe_identifier(target_version):
        raise _config_error(path, "target_version is not a safe identifier")
    if target_version not in versions:
        raise _config_error(path, "target_version must occur in versions")

    return list(versions), target_version


def _version_index(versions, version, label):
    try:
        return versions.index(version)
    except ValueError as exc:
        raise UnknownVersionError(f"Unknown {label} {version!r}") from exc


def compare_version_positions(versions, target_version, current_version):
    target_index = _version_index(versions, target_version, "target version")
    current_index = _version_index(versions, current_version, "current version")
    return (current_index > target_index) - (current_index < target_index)


def select_upgrade_versions(versions, target_version, current_version):
    relation = compare_version_positions(versions, target_version, current_version)
    if relation >= 0:
        return []

    current_index = versions.index(current_version)
    target_index = versions.index(target_version)
    return list(versions[current_index + 1 : target_index + 1])
