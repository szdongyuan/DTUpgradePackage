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
