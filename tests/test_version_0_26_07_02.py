from pathlib import Path

import yaml

from consts.program_config import ALL_VERSIONS, TARGET_VERSION


def test_version_manifest_contains_only_empty_upgrade_operations():
    manifest_path = (
        Path(__file__).resolve().parents[1] / "resources" / "version_0.26.07.02.yml"
    )

    assert yaml.safe_load(manifest_path.read_text(encoding="utf-8")) == {
        "version": "0.26.07.02",
        "db": {
            "sqls_operate": [],
            "data_check": {"add": [], "del": []},
        },
        "ai": [],
        "ui": [],
        "config": [],
        "json_merge": [],
        "exe": [],
    }


def test_external_version_file_defines_current_release():
    expected_versions = [
        "0.12",
        "0.25.06.01",
        "0.25.07.01",
        "0.25.09.01",
        "0.25.12.01",
        "0.26.02.01",
        "0.26.06.01",
        "0.26.07.01",
        "0.26.07.02",
    ]
    expected_target = "0.26.07.02"
    path = Path(__file__).resolve().parents[1] / "resources" / "version.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert data["versions"] == expected_versions
    assert ALL_VERSIONS == expected_versions
    assert data["target_version"] == expected_target
    assert TARGET_VERSION == expected_target
