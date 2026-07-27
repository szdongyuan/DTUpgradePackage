import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from base.json_merge import merge_json_files_with_reason as real_merge_json_files_with_reason
from control import upgrade_config


class DummyLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))


def directory_path(path):
    return str(path) + os.sep


@pytest.fixture
def configured_tree(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    package_dir = tmp_path / "package"
    config_dir = package_dir / "resources" / "config"
    backup_dir = package_dir / "resources" / "backup"
    roots = {
        "ui_pic": target_dir / "ui" / "ui_pic",
        "ui_config": target_dir / "ui" / "ui_config",
        "ai_model_config": target_dir / "configs" / "aimodel_config",
        "scanner_barcode_config": target_dir / "configs" / "scanner_barcode_config",
    }
    backups = {
        "ui_pic": backup_dir / "ui_pic",
        "ui_config": backup_dir / "ui_config",
        "ai_model_config": backup_dir / "ai_model_config",
        "scanner_barcode_config": backup_dir / "scanner_barcode_config",
    }

    for name, root in roots.items():
        root.mkdir(parents=True)
        (root / "marker.txt").write_text(f"original {name}", encoding="utf-8")

    config_dir.mkdir(parents=True)
    monkeypatch.setattr(upgrade_config, "DEFAULT_DIR", directory_path(package_dir))
    monkeypatch.setattr(upgrade_config, "DEFAULT_TARGET_DIR", directory_path(target_dir))
    monkeypatch.setattr(upgrade_config, "CONFIG_DIR", directory_path(config_dir))
    monkeypatch.setattr(upgrade_config, "BACKUP_DIR", directory_path(backup_dir))
    monkeypatch.setattr(upgrade_config, "UI_PIC_PATH", directory_path(roots["ui_pic"]))
    monkeypatch.setattr(upgrade_config, "UI_CONFIG_PATH", directory_path(roots["ui_config"]))
    monkeypatch.setattr(upgrade_config, "AI_MODEL_CONFIG_PATH", directory_path(roots["ai_model_config"]))
    monkeypatch.setattr(
        upgrade_config,
        "SCANNER_BARCODE_CONFIG_PATH",
        directory_path(roots["scanner_barcode_config"]),
    )
    monkeypatch.setattr(upgrade_config, "BACKUP_UI_PIC_PATH", directory_path(backups["ui_pic"]))
    monkeypatch.setattr(upgrade_config, "BACKUP_UI_CONFIG_PATH", directory_path(backups["ui_config"]))
    monkeypatch.setattr(
        upgrade_config,
        "BACKUP_AI_MODEL_CONFIG_PATH",
        directory_path(backups["ai_model_config"]),
    )
    monkeypatch.setattr(
        upgrade_config,
        "BACKUP_SCANNER_BARCODE_CONFIG_PATH",
        directory_path(backups["scanner_barcode_config"]),
    )

    return SimpleNamespace(
        package_dir=package_dir,
        config_dir=config_dir,
        target_dir=target_dir,
        backup_dir=backup_dir,
        roots=roots,
        backups=backups,
    )


def make_manager():
    return upgrade_config.ConfigManager(DummyLogger(), "99")


def logged_messages(manager, level=None):
    return [
        message
        for message_level, message in manager.logger.messages
        if level is None or message_level == level
    ]


def create_backup_roots(configured_tree, names):
    for name in names:
        backup = configured_tree.backups[name]
        backup.mkdir(parents=True)
        (backup / "marker.txt").write_text(f"backup {name}", encoding="utf-8")


def snapshot_tree(root):
    return {
        path.relative_to(root).as_posix(): (
            "directory" if path.is_dir() else path.read_bytes()
        )
        for path in sorted(root.rglob("*"))
    }


def swap_artifacts(destination):
    prefixes = (
        f".{destination.name}.staging-",
        f".{destination.name}.old-",
    )
    return sorted(
        path.name
        for path in destination.parent.iterdir()
        if path.name.startswith(prefixes)
    )


def write_version_manifest(configured_tree, version, data):
    version_path = configured_tree.package_dir / "resources" / f"version_{version}.yml"
    version_path.parent.mkdir(parents=True, exist_ok=True)
    version_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return version_path


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def configure_manifest_manager(configured_tree, manifests):
    manager = make_manager()
    manager.upgrade_version_list = []
    for version, data in manifests:
        write_version_manifest(configured_tree, version, data)
        manager.upgrade_version_list.append(version)
    return manager


def json_merge_messages(manager, level=None):
    return [
        message
        for message in logged_messages(manager, level)
        if message.startswith("json_merge")
    ]


def test_successful_managed_root_backup_changes_only_matching_flag(configured_tree):
    manager = make_manager()

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is True

    assert manager.ui_config_flag is False
    assert manager.ui_pic_flag is True
    assert manager.ai_model_config_flag is True
    assert manager.scannre_barcode_config_flag is True
    assert (configured_tree.backups["ui_config"] / "marker.txt").read_text(encoding="utf-8") == (
        "original ui_config"
    )


def test_failed_managed_root_backup_keeps_flag_and_reports_failure(configured_tree, monkeypatch):
    manager = make_manager()
    copy_calls = []

    def fail_copy(source, destination):
        copy_calls.append((source, destination))
        return False

    monkeypatch.setattr(manager, "smart_copy", fail_copy)

    assert manager.backup_config_file(["configs/aimodel_config/model.json"]) is False

    assert len(copy_calls) == 1
    assert copy_calls[0][0] == upgrade_config.AI_MODEL_CONFIG_PATH
    assert Path(copy_calls[0][1]).parent == configured_tree.backup_dir
    assert Path(copy_calls[0][1]).name.startswith(".ai_model_config.staging-")
    assert manager.ai_model_config_flag is True
    assert swap_artifacts(configured_tree.backups["ai_model_config"]) == []
    assert not any("配置文件备份已保存到" in message for message in logged_messages(manager))
    assert any("备份" in message for message in logged_messages(manager, "error"))


def test_backup_first_swap_move_failure_preserves_existing_data_and_flag(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    backup_root = configured_tree.backups["ui_config"]
    backup_root.joinpath("nested").mkdir(parents=True)
    backup_root.joinpath("stale.txt").write_bytes(b"recovery data")
    backup_root.joinpath("nested", "retained.bin").write_bytes(b"\x00\xffrecovery")
    expected_backup = snapshot_tree(backup_root)
    real_replace = os.replace

    def fail_existing_destination_move(source, destination):
        if (
            Path(source) == backup_root
            and Path(destination).name.startswith(f".{backup_root.name}.old-")
        ):
            raise OSError("injected existing destination move failure")
        return real_replace(source, destination)

    monkeypatch.setattr(upgrade_config.os, "replace", fail_existing_destination_move)

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is False

    assert manager.ui_config_flag is True
    assert snapshot_tree(backup_root) == expected_backup
    assert swap_artifacts(backup_root) == []


def test_backup_copy_failure_preserves_preexisting_snapshot_and_flag(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    backup_root = configured_tree.backups["ui_config"]
    backup_root.joinpath("nested").mkdir(parents=True)
    backup_root.joinpath("marker.txt").write_bytes(b"prior recovery bytes")
    backup_root.joinpath("nested", "retained.bin").write_bytes(b"\x00\xffrecovery")
    expected_backup = snapshot_tree(backup_root)
    copy_calls = []

    def partially_fail_copy(source, destination):
        copy_calls.append((source, destination))
        Path(destination).mkdir(parents=True, exist_ok=True)
        Path(destination).joinpath("partial.bin").write_bytes(b"partial")
        return False

    monkeypatch.setattr(manager, "smart_copy", partially_fail_copy)

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is False

    assert manager.ui_config_flag is True
    assert len(copy_calls) == 1
    assert copy_calls[0][0] == upgrade_config.UI_CONFIG_PATH
    assert Path(copy_calls[0][1]).parent == backup_root.parent
    assert Path(copy_calls[0][1]) != backup_root
    assert snapshot_tree(backup_root) == expected_backup
    assert swap_artifacts(backup_root) == []


def test_managed_root_is_backed_up_only_once_for_multiple_files(configured_tree, monkeypatch):
    manager = make_manager()
    copy_calls = []

    def successful_copy(source, destination):
        copy_calls.append((source, destination))
        return True

    monkeypatch.setattr(manager, "smart_copy", successful_copy)

    assert manager.backup_config_file(
        [
            "ui/ui_config/first.json",
            "ui/ui_config/second.json",
        ]
    ) is True

    assert len(copy_calls) == 1
    assert copy_calls[0][0] == upgrade_config.UI_CONFIG_PATH
    assert Path(copy_calls[0][1]).parent == configured_tree.backup_dir
    assert Path(copy_calls[0][1]).name.startswith(".ui_config.staging-")
    assert manager.ui_config_flag is False


def test_managed_root_backup_replaces_stale_preexisting_snapshot(configured_tree):
    manager = make_manager()
    backup_root = configured_tree.backups["ui_config"]
    target_root = configured_tree.roots["ui_config"]
    stale_backup_file = backup_root / "stale.txt"

    backup_root.mkdir(parents=True)
    stale_backup_file.write_text("stale backup data", encoding="utf-8")

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is True

    assert not stale_backup_file.exists()
    target_root.joinpath("marker.txt").write_text("changed", encoding="utf-8")

    assert manager.restore_config() is True

    assert not target_root.joinpath("stale.txt").exists()
    assert target_root.joinpath("marker.txt").read_text(encoding="utf-8") == (
        "original ui_config"
    )


def test_successful_restore_returns_true_and_deletes_backup(configured_tree):
    manager = make_manager()
    target_marker = configured_tree.roots["ui_config"] / "marker.txt"

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is True
    target_marker.write_text("changed", encoding="utf-8")

    assert manager.restore_config() is True

    assert target_marker.read_text(encoding="utf-8") == "original ui_config"
    assert not configured_tree.backups["ui_config"].exists()


def test_successful_restore_removes_upgrade_created_target_only_file(configured_tree):
    manager = make_manager()
    target_root = configured_tree.roots["ui_config"]
    upgrade_created_file = target_root / "upgrade-created.txt"

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is True
    upgrade_created_file.write_text("created during upgrade", encoding="utf-8")

    assert manager.restore_config() is True

    assert not upgrade_created_file.exists()
    assert target_root.joinpath("marker.txt").read_text(encoding="utf-8") == (
        "original ui_config"
    )
    assert not configured_tree.backups["ui_config"].exists()


def test_restore_copy_failure_preserves_live_target_and_backup(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    target_root = configured_tree.roots["ui_config"]
    backup_root = configured_tree.backups["ui_config"]
    create_backup_roots(configured_tree, ["ui_config"])
    manager.ui_config_flag = False
    target_root.joinpath("marker.txt").write_bytes(b"current live bytes")
    target_root.joinpath("upgrade-created.bin").write_bytes(b"\x00live")
    backup_root.joinpath("recovery-only.bin").write_bytes(b"\xffbackup")
    expected_target = snapshot_tree(target_root)
    expected_backup = snapshot_tree(backup_root)
    copy_calls = []

    def partially_fail_copy(source, destination):
        copy_calls.append((source, destination))
        Path(destination).mkdir(parents=True, exist_ok=True)
        Path(destination).joinpath("partial.bin").write_bytes(b"partial")
        return False

    monkeypatch.setattr(manager, "smart_copy", partially_fail_copy)

    assert manager.restore_config() is False

    assert len(copy_calls) == 1
    assert copy_calls[0][0] == upgrade_config.BACKUP_UI_CONFIG_PATH
    assert Path(copy_calls[0][1]).parent == target_root.parent
    assert Path(copy_calls[0][1]) != target_root
    assert snapshot_tree(target_root) == expected_target
    assert snapshot_tree(backup_root) == expected_backup
    assert swap_artifacts(target_root) == []


def test_restore_swap_failure_rolls_back_live_target_and_retains_backup(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    target_root = configured_tree.roots["ui_config"]
    backup_root = configured_tree.backups["ui_config"]
    create_backup_roots(configured_tree, ["ui_config"])
    manager.ui_config_flag = False
    target_root.joinpath("marker.txt").write_bytes(b"current live bytes")
    target_root.joinpath("upgrade-created.bin").write_bytes(b"\x00live")
    backup_root.joinpath("recovery-only.bin").write_bytes(b"\xffbackup")
    expected_target = snapshot_tree(target_root)
    expected_backup = snapshot_tree(backup_root)
    real_replace = os.replace
    replace_calls = []

    def fail_staging_install(source, destination):
        replace_calls.append((Path(source), Path(destination)))
        if (
            Path(destination) == target_root
            and Path(source).name.startswith(f".{target_root.name}.staging-")
        ):
            raise OSError("injected staging install failure")
        return real_replace(source, destination)

    monkeypatch.setattr(upgrade_config.os, "replace", fail_staging_install)

    assert manager.restore_config() is False

    assert len(replace_calls) == 3
    assert snapshot_tree(target_root) == expected_target
    assert snapshot_tree(backup_root) == expected_backup
    assert swap_artifacts(target_root) == []


def test_restore_failure_attempts_all_selected_roots_and_retains_backups(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    selected = [
        ("ui_pic", "ui_pic_flag", upgrade_config.BACKUP_UI_PIC_PATH, upgrade_config.UI_PIC_PATH),
        (
            "ai_model_config",
            "ai_model_config_flag",
            upgrade_config.BACKUP_AI_MODEL_CONFIG_PATH,
            upgrade_config.AI_MODEL_CONFIG_PATH,
        ),
        (
            "scanner_barcode_config",
            "scannre_barcode_config_flag",
            upgrade_config.BACKUP_SCANNER_BARCODE_CONFIG_PATH,
            upgrade_config.SCANNER_BARCODE_CONFIG_PATH,
        ),
        (
            "ui_config",
            "ui_config_flag",
            upgrade_config.BACKUP_UI_CONFIG_PATH,
            upgrade_config.UI_CONFIG_PATH,
        ),
    ]
    create_backup_roots(configured_tree, [name for name, *_rest in selected])
    for _name, flag, _source, _destination in selected:
        setattr(manager, flag, False)

    copy_calls = []
    delete_calls = []

    def copy_with_first_failure(source, destination):
        copy_calls.append((source, destination))
        return source != upgrade_config.BACKUP_UI_PIC_PATH

    def track_delete():
        delete_calls.append(True)
        return True

    monkeypatch.setattr(manager, "smart_copy", copy_with_first_failure)
    monkeypatch.setattr(manager, "delete_backup_file", track_delete)

    assert manager.restore_config() is False

    assert [source for source, _staging in copy_calls] == [
        source for _name, _flag, source, _destination in selected
    ]
    for (
        (_name, _flag, _source, destination),
        (_copied_source, staging),
    ) in zip(selected, copy_calls):
        destination = Path(destination)
        staging = Path(staging)
        assert staging.parent == destination.parent
        assert staging.name.startswith(f".{destination.name}.staging-")
    assert delete_calls == []
    assert all(configured_tree.backups[name].exists() for name, *_rest in selected)
    assert any("恢复" in message for message in logged_messages(manager, "error"))
    assert "ui图片配置文件已恢复" not in logged_messages(manager, "info")


def test_restore_returns_false_when_cleanup_partially_fails_and_attempts_all_deletions(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    selected_names = ["ai_model_config", "ui_config"]
    create_backup_roots(configured_tree, selected_names)
    manager.ai_model_config_flag = False
    manager.ui_config_flag = False
    remove_calls = []
    real_smart_remove = manager.smart_remove

    monkeypatch.setattr(manager, "smart_copy", lambda _source, _destination: True)

    def remove_with_ai_failure(path):
        if path in (
            upgrade_config.BACKUP_AI_MODEL_CONFIG_PATH,
            upgrade_config.BACKUP_UI_CONFIG_PATH,
        ):
            remove_calls.append(path)
        if path == upgrade_config.BACKUP_AI_MODEL_CONFIG_PATH:
            return False
        return real_smart_remove(path)

    monkeypatch.setattr(manager, "smart_remove", remove_with_ai_failure)

    assert manager.restore_config() is False

    assert remove_calls == [
        upgrade_config.BACKUP_AI_MODEL_CONFIG_PATH,
        upgrade_config.BACKUP_UI_CONFIG_PATH,
    ]
    assert configured_tree.backups["ai_model_config"].exists()
    assert not configured_tree.backups["ui_config"].exists()
    assert not any(
        "ai模型配置备份文件已删除" in message for message in logged_messages(manager, "info")
    )


@pytest.mark.parametrize(
    "manifest",
    [
        {"config": []},
        {"config": [], "json_merge": []},
    ],
    ids=["missing-section", "empty-section"],
)
def test_missing_or_empty_json_merge_is_a_compatible_noop(
    configured_tree,
    monkeypatch,
    manifest,
):
    helper_calls = []

    def unexpected_merge(*args):
        helper_calls.append(args)
        return True, "ok"

    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        unexpected_merge,
        raising=False,
    )
    manager = configure_manifest_manager(configured_tree, [("1.0", manifest)])

    assert manager.update_config_file() is True
    assert helper_calls == []


def test_existing_config_actions_run_before_json_merge_actions(
    configured_tree,
    monkeypatch,
):
    replacement_relative = "ui/ui_config/replaced.json"
    merge_relative = "ui/ui_config/merged.json"
    write_json(configured_tree.roots["ui_config"] / "replaced.json", {"old": True})
    write_json(configured_tree.config_dir / "replaced.json", {"new": True})
    write_json(configured_tree.roots["ui_config"] / "merged.json", {"target": True})
    write_json(configured_tree.config_dir / "patch.json", {"source": True})
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [replacement_relative],
                    "json_merge": [
                        {
                            "target": merge_relative,
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            )
        ],
    )
    events = []

    def track_backup(paths):
        events.append(("backup", tuple(paths)))
        return True

    def track_remove(path):
        events.append(("remove", path))
        return True

    def track_copy(source, target):
        events.append(("copy", source, target))
        return True

    def track_merge(target, source):
        events.append(("merge", target, source))
        return True, "ok"

    monkeypatch.setattr(manager, "backup_config_file", track_backup)
    monkeypatch.setattr(manager, "smart_remove", track_remove)
    monkeypatch.setattr(manager, "smart_copy", track_copy)
    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        track_merge,
        raising=False,
    )

    assert manager.update_config_file() is True

    event_names = [event[0] for event in events]
    assert event_names == ["backup", "remove", "copy", "backup", "merge"]
    assert events[0][1] == (replacement_relative,)
    assert events[3][1] == (merge_relative,)


@pytest.mark.parametrize(
    ("raw_section", "schema_category"),
    [
        ("not-a-list", "section_must_be_list"),
        (None, "section_must_be_list"),
        ([42], "entry_must_be_object"),
        ([{"source": "resources/config/patch.json"}], "entry_fields_invalid"),
        ([{"target": "ui/ui_config/target.json"}], "entry_fields_invalid"),
        (
            [
                {
                    "target": "ui/ui_config/target.json",
                    "source": "resources/config/patch.json",
                    "unexpected": "TOP_SECRET_UNKNOWN_VALUE",
                }
            ],
            "entry_fields_invalid",
        ),
        (
            [{"target": "", "source": "resources/config/patch.json"}],
            "target_must_be_non_empty_string",
        ),
        (
            [{"target": "   ", "source": "resources/config/patch.json"}],
            "target_must_be_non_empty_string",
        ),
        (
            [{"target": 7, "source": "resources/config/patch.json"}],
            "target_must_be_non_empty_string",
        ),
        (
            [{"target": "ui/ui_config/target.json", "source": ""}],
            "source_must_be_non_empty_string",
        ),
        (
            [{"target": "ui/ui_config/target.json", "source": []}],
            "source_must_be_non_empty_string",
        ),
    ],
)
def test_malformed_json_merge_schema_fails_with_non_sensitive_category_log(
    configured_tree,
    raw_section,
    schema_category,
):
    manager = configure_manifest_manager(
        configured_tree,
        [("1.0", {"config": [], "json_merge": raw_section})],
    )

    assert manager.update_config_file() is False

    errors = "\n".join(json_merge_messages(manager, "error"))
    assert schema_category in errors
    assert "TOP_SECRET_UNKNOWN_VALUE" not in errors


def test_config_and_json_merge_target_conflict_fails_before_merge_write(
    configured_tree,
    monkeypatch,
):
    target_relative = "ui/ui_config/same.json"
    target = write_json(
        configured_tree.roots["ui_config"] / "same.json",
        {"state": "pre-upgrade"},
    )
    write_json(configured_tree.config_dir / "same.json", {"state": "replacement"})
    write_json(configured_tree.config_dir / "patch.json", {"added": True})
    helper_calls = []
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [target_relative],
                    "json_merge": [
                        {
                            "target": target_relative,
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            )
        ],
    )

    def unexpected_merge(*args):
        helper_calls.append(args)
        return True, "ok"

    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        unexpected_merge,
        raising=False,
    )

    assert manager.update_config_file() is False

    assert helper_calls == []
    assert json.loads(target.read_text(encoding="utf-8")) == {"state": "replacement"}
    assert any(
        "target_conflicts_with_config" in message
        for message in json_merge_messages(manager, "error")
    )


@pytest.mark.parametrize(
    ("case_name", "expected_category"),
    [
        ("absolute_target", "target_path:absolute_path"),
        ("absolute_source", "source_path:absolute_path"),
        ("target_parent_component", "target_path:parent_reference"),
        ("source_parent_component", "source_path:parent_reference"),
        ("target_outside_managed_roots", "target_path:outside_allowed_root"),
        ("source_outside_config", "source_path:outside_allowed_root"),
        ("missing_target", "target_path:missing_file"),
        ("missing_source", "source_path:missing_file"),
        ("target_is_directory", "target_path:missing_file"),
        ("source_is_directory", "source_path:missing_file"),
    ],
)
def test_json_merge_rejects_unsafe_missing_and_non_file_paths(
    configured_tree,
    case_name,
    expected_category,
):
    target = write_json(
        configured_tree.roots["ui_config"] / "target.json",
        {"target": "TOP_SECRET_TARGET"},
    )
    source = write_json(
        configured_tree.config_dir / "patch.json",
        {"source": "TOP_SECRET_SOURCE"},
    )
    (configured_tree.roots["ui_config"] / "sub").mkdir()
    (configured_tree.config_dir / "sub").mkdir()
    outside_target = write_json(
        configured_tree.roots["ui_pic"] / "outside.json",
        {"outside": True},
    )
    outside_source = write_json(
        configured_tree.package_dir / "resources" / "outside.json",
        {"outside": True},
    )
    target_directory = configured_tree.roots["ui_config"] / "directory"
    source_directory = configured_tree.config_dir / "directory"
    target_directory.mkdir()
    source_directory.mkdir()

    target_relative = "ui/ui_config/target.json"
    source_relative = "resources/config/patch.json"
    if case_name == "absolute_target":
        target_relative = str(target)
    elif case_name == "absolute_source":
        source_relative = str(source)
    elif case_name == "target_parent_component":
        target_relative = "ui/ui_config/sub/../target.json"
    elif case_name == "source_parent_component":
        source_relative = "resources/config/sub/../patch.json"
    elif case_name == "target_outside_managed_roots":
        target_relative = "ui/ui_pic/outside.json"
    elif case_name == "source_outside_config":
        source_relative = "resources/outside.json"
    elif case_name == "missing_target":
        target_relative = "ui/ui_config/missing.json"
    elif case_name == "missing_source":
        source_relative = "resources/config/missing.json"
    elif case_name == "target_is_directory":
        target_relative = "ui/ui_config/directory"
    elif case_name == "source_is_directory":
        source_relative = "resources/config/directory"

    target_bytes = target.read_bytes()
    source_bytes = source.read_bytes()
    outside_target_bytes = outside_target.read_bytes()
    outside_source_bytes = outside_source.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [],
                    "json_merge": [
                        {"target": target_relative, "source": source_relative}
                    ],
                },
            )
        ],
    )

    assert manager.update_config_file() is False

    assert target.read_bytes() == target_bytes
    assert source.read_bytes() == source_bytes
    assert outside_target.read_bytes() == outside_target_bytes
    assert outside_source.read_bytes() == outside_source_bytes
    errors = "\n".join(json_merge_messages(manager, "error"))
    assert expected_category in errors
    assert "TOP_SECRET_TARGET" not in errors
    assert "TOP_SECRET_SOURCE" not in errors


def create_symlink_or_skip(source, link, target_is_directory=False):
    try:
        os.symlink(source, link, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")


@pytest.mark.parametrize("escape_kind", ["target", "source"])
def test_json_merge_rejects_symlinks_that_resolve_outside_allowed_root(
    configured_tree,
    escape_kind,
):
    target = configured_tree.roots["ui_config"] / "target.json"
    source = configured_tree.config_dir / "patch.json"
    target_relative = "ui/ui_config/target.json"
    source_relative = "resources/config/patch.json"

    if escape_kind == "target":
        outside = write_json(
            configured_tree.target_dir.parent / "outside-target.json",
            {"secret": "TOP_SECRET_OUTSIDE_TARGET"},
        )
        create_symlink_or_skip(outside, target)
    else:
        write_json(target, {"target": True})
        outside = write_json(
            configured_tree.package_dir / "outside-source.json",
            {"secret": "TOP_SECRET_OUTSIDE_SOURCE"},
        )
        create_symlink_or_skip(outside, source)

    if escape_kind == "target":
        write_json(source, {"source": True})

    outside_bytes = outside.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [],
                    "json_merge": [
                        {"target": target_relative, "source": source_relative}
                    ],
                },
            )
        ],
    )

    assert manager.update_config_file() is False

    assert outside.read_bytes() == outside_bytes
    assert any(
        f"{escape_kind}_path:symlink_escape" in message
        for message in json_merge_messages(manager, "error")
    )
    assert "TOP_SECRET_OUTSIDE" not in "\n".join(
        json_merge_messages(manager, "error")
    )


@pytest.mark.parametrize("inward_kind", ["target", "source"])
def test_json_merge_rejects_lexical_paths_outside_roots_even_when_symlink_resolves_inward(
    configured_tree,
    inward_kind,
):
    target = write_json(
        configured_tree.roots["ui_config"] / "target.json",
        {"target": True},
    )
    source = write_json(configured_tree.config_dir / "patch.json", {"source": True})
    target_relative = "ui/ui_config/target.json"
    source_relative = "resources/config/patch.json"

    if inward_kind == "target":
        create_symlink_or_skip(
            configured_tree.roots["ui_config"],
            configured_tree.target_dir / "ui-config-link",
            target_is_directory=True,
        )
        target_relative = "ui-config-link/target.json"
    else:
        create_symlink_or_skip(
            configured_tree.config_dir,
            configured_tree.package_dir / "resources" / "config-link",
            target_is_directory=True,
        )
        source_relative = "resources/config-link/patch.json"

    target_bytes = target.read_bytes()
    source_bytes = source.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [],
                    "json_merge": [
                        {"target": target_relative, "source": source_relative}
                    ],
                },
            )
        ],
    )

    assert manager.update_config_file() is False

    assert target.read_bytes() == target_bytes
    assert source.read_bytes() == source_bytes
    assert any(
        f"{inward_kind}_path:outside_allowed_root" in message
        for message in json_merge_messages(manager, "error")
    )


def test_json_merge_rejects_target_source_same_file_alias(
    configured_tree,
):
    target = write_json(
        configured_tree.roots["ui_config"] / "target.json",
        {"target": True},
    )
    source = configured_tree.config_dir / "patch.json"
    try:
        os.link(target, source)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    original_bytes = target.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/target.json",
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            )
        ],
    )

    assert manager.update_config_file() is False

    assert target.read_bytes() == original_bytes
    assert source.read_bytes() == original_bytes
    assert any(
        "target_source_alias" in message
        for message in json_merge_messages(manager, "error")
    )


def test_json_merges_run_in_entry_and_version_order_with_one_backup_before_writes(
    configured_tree,
    monkeypatch,
):
    target_a = write_json(
        configured_tree.roots["ui_config"] / "a.json",
        {"shared": {"winner": "target"}, "target_only": 1},
    )
    target_b = write_json(
        configured_tree.roots["ui_config"] / "b.json",
        {"b_target": True},
    )
    source_a1 = write_json(
        configured_tree.config_dir / "a1.json",
        {"from_v1": 1, "shared": {"winner": "source", "extra": "not-added"}},
    )
    source_b1 = write_json(
        configured_tree.config_dir / "b1.json",
        {"b_source": True},
    )
    source_a2 = write_json(
        configured_tree.config_dir / "a2.json",
        {"from_v2": 2, "from_v1": 999},
    )
    source_bytes = {
        path: path.read_bytes()
        for path in (source_a1, source_b1, source_a2)
    }
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/a.json",
                            "source": "resources/config/a1.json",
                        },
                        {
                            "target": "ui/ui_config/b.json",
                            "source": "resources/config/b1.json",
                        },
                    ],
                },
            ),
            (
                "2.0",
                {
                    "config": [],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/a.json",
                            "source": "resources/config/a2.json",
                        }
                    ],
                },
            ),
        ],
    )
    events = []
    real_snapshot = manager._replace_with_staged_snapshot

    def track_snapshot(source, destination):
        if source == upgrade_config.UI_CONFIG_PATH:
            events.append("backup")
        return real_snapshot(source, destination)

    def track_merge(target_path, source_path):
        events.append(f"merge:{Path(target_path).name}:{Path(source_path).name}")
        return real_merge_json_files_with_reason(target_path, source_path)

    monkeypatch.setattr(manager, "_replace_with_staged_snapshot", track_snapshot)
    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        track_merge,
        raising=False,
    )

    assert manager.update_config_file() is True

    assert events == [
        "backup",
        "merge:a.json:a1.json",
        "merge:b.json:b1.json",
        "merge:a.json:a2.json",
    ]
    assert json.loads(target_a.read_text(encoding="utf-8")) == {
        "from_v2": 2,
        "from_v1": 1,
        "shared": {"winner": "target"},
        "target_only": 1,
    }
    assert json.loads(target_b.read_text(encoding="utf-8")) == {
        "b_source": True,
        "b_target": True,
    }
    assert all(path.read_bytes() == content for path, content in source_bytes.items())
    assert manager.ui_config_flag is False
    assert configured_tree.backups["ui_config"].is_dir()


def test_later_invalid_merge_can_restore_exact_pre_upgrade_target_bytes(
    configured_tree,
):
    target = write_json(
        configured_tree.roots["ui_config"] / "target.json",
        {"target": "authoritative"},
    )
    source = write_json(
        configured_tree.config_dir / "valid.json",
        {"added": "first merge"},
    )
    invalid_source = configured_tree.config_dir / "invalid.json"
    invalid_source.write_bytes(b'{"TOP_SECRET_INVALID_JSON": ')
    original_target_bytes = target.read_bytes()
    original_source_bytes = source.read_bytes()
    original_invalid_source_bytes = invalid_source.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/target.json",
                            "source": "resources/config/valid.json",
                        }
                    ],
                },
            ),
            (
                "2.0",
                {
                    "config": [],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/target.json",
                            "source": "resources/config/invalid.json",
                        }
                    ],
                },
            ),
        ],
    )

    assert manager.update_config_file() is False
    assert target.read_bytes() != original_target_bytes
    assert manager.ui_config_flag is False
    assert configured_tree.backups["ui_config"].is_dir()

    assert manager.restore_config() is True

    assert target.read_bytes() == original_target_bytes
    assert source.read_bytes() == original_source_bytes
    assert invalid_source.read_bytes() == original_invalid_source_bytes
    assert not configured_tree.backups["ui_config"].exists()
    errors = "\n".join(json_merge_messages(manager, "error"))
    assert "invalid_json" in errors
    assert "TOP_SECRET_INVALID_JSON" not in errors


def test_json_merge_backup_failure_prevents_helper_call_and_preserves_inputs(
    configured_tree,
    monkeypatch,
):
    target = write_json(
        configured_tree.roots["ui_config"] / "target.json",
        {"target": "TOP_SECRET_TARGET"},
    )
    source = write_json(
        configured_tree.config_dir / "patch.json",
        {"source": "TOP_SECRET_SOURCE"},
    )
    target_bytes = target.read_bytes()
    source_bytes = source.read_bytes()
    helper_calls = []
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/target.json",
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            )
        ],
    )

    monkeypatch.setattr(manager, "backup_config_file", lambda paths: not paths)

    def unexpected_merge(*args):
        helper_calls.append(args)
        return True, "ok"

    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        unexpected_merge,
        raising=False,
    )

    assert manager.update_config_file() is False

    assert helper_calls == []
    assert target.read_bytes() == target_bytes
    assert source.read_bytes() == source_bytes
    errors = "\n".join(json_merge_messages(manager, "error"))
    assert "backup_failed" in errors
    assert "TOP_SECRET_TARGET" not in errors
    assert "TOP_SECRET_SOURCE" not in errors


@pytest.mark.parametrize(
    ("reason", "source_bytes"),
    [
        ("invalid_json", b'{"TOP_SECRET_INVALID_JSON": '),
        ("non_object_root", b'["TOP_SECRET_NON_OBJECT"]'),
        ("write_failed", b'{"TOP_SECRET_WRITE": true}'),
    ],
)
def test_json_merge_helper_failure_reason_is_logged_without_json_contents(
    configured_tree,
    monkeypatch,
    reason,
    source_bytes,
):
    target = write_json(
        configured_tree.roots["ui_config"] / "target.json",
        {"target": "TOP_SECRET_TARGET"},
    )
    source = configured_tree.config_dir / "patch.json"
    source.write_bytes(source_bytes)
    target_bytes = target.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/target.json",
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            )
        ],
    )

    if reason == "write_failed":
        monkeypatch.setattr(
            upgrade_config,
            "merge_json_files_with_reason",
            lambda _target, _source: (False, "write_failed"),
            raising=False,
        )

    assert manager.update_config_file() is False

    assert target.read_bytes() == target_bytes
    assert source.read_bytes() == source_bytes
    errors = "\n".join(json_merge_messages(manager, "error"))
    assert reason in errors
    for sensitive_text in (
        "TOP_SECRET_INVALID_JSON",
        "TOP_SECRET_NON_OBJECT",
        "TOP_SECRET_WRITE",
        "TOP_SECRET_TARGET",
    ):
        assert sensitive_text not in errors


def test_json_merge_start_and_success_logs_and_prints_only_relative_paths(
    configured_tree,
    capsys,
):
    target_relative = "ui/ui_config/target.json"
    source_relative = "resources/config/patch.json"
    write_json(configured_tree.roots["ui_config"] / "target.json", {"target": True})
    write_json(configured_tree.config_dir / "patch.json", {"source": True})
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [],
                    "json_merge": [
                        {"target": target_relative, "source": source_relative}
                    ],
                },
            )
        ],
    )

    assert manager.update_config_file() is True

    messages = json_merge_messages(manager, "info")
    assert any(
        "start" in message
        and target_relative in message
        and source_relative in message
        for message in messages
    )
    assert any(
        "success" in message
        and target_relative in message
        and source_relative in message
        for message in messages
    )
    for message in messages:
        assert str(configured_tree.target_dir) not in message
        assert str(configured_tree.package_dir) not in message

    printed_merge_lines = [
        line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("json_merge")
    ]
    assert any("start" in line for line in printed_merge_lines)
    assert any("success" in line for line in printed_merge_lines)
    for line in printed_merge_lines:
        assert str(configured_tree.target_dir) not in line
        assert str(configured_tree.package_dir) not in line
