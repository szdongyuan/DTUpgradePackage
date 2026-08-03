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


def test_version_is_latest_target_after_0_26_07_01():
    assert ALL_VERSIONS[-2:] == ["0.26.07.01", "0.26.07.02"]
    assert TARGET_VERSION == "0.26.07.02"
