import os
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    backup_dir = tmp_path / "package" / "resources" / "backup"
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

    monkeypatch.setattr(upgrade_config, "DEFAULT_TARGET_DIR", directory_path(target_dir))
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

    return SimpleNamespace(target_dir=target_dir, backup_dir=backup_dir, roots=roots, backups=backups)


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

    assert copy_calls == [
        (
            upgrade_config.AI_MODEL_CONFIG_PATH,
            upgrade_config.BACKUP_AI_MODEL_CONFIG_PATH,
        )
    ]
    assert manager.ai_model_config_flag is True
    assert not any("配置文件备份已保存到" in message for message in logged_messages(manager))
    assert any("备份" in message for message in logged_messages(manager, "error"))


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

    assert copy_calls == [
        (
            upgrade_config.UI_CONFIG_PATH,
            upgrade_config.BACKUP_UI_CONFIG_PATH,
        )
    ]
    assert manager.ui_config_flag is False


def test_successful_restore_returns_true_and_deletes_backup(configured_tree):
    manager = make_manager()
    target_marker = configured_tree.roots["ui_config"] / "marker.txt"

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is True
    target_marker.write_text("changed", encoding="utf-8")

    assert manager.restore_config() is True

    assert target_marker.read_text(encoding="utf-8") == "original ui_config"
    assert not configured_tree.backups["ui_config"].exists()


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

    assert copy_calls == [(source, destination) for _name, _flag, source, destination in selected]
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
