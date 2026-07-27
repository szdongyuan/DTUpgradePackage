import os
from types import SimpleNamespace

import pytest
import yaml

import main as upgrade_main
from control import upgrade_config


class RecordingLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))


def _perform_behavior(behavior, name, on_success=None):
    if behavior == "raise":
        raise RuntimeError(f"{name} raised")
    if behavior == "false":
        return False
    if on_success is not None:
        on_success()
    return True


def install_fake_upgrade(
    monkeypatch,
    *,
    failure_stage=None,
    failure_mode="false",
    database_restore="success",
    config_restore="success",
    database_cleanup="success",
    config_cleanup="success",
):
    events = []
    logger = RecordingLogger()
    logger_factory_calls = []
    recovery_files = {"database": True, "config": True}
    received_loggers = []

    def stage_result(stage):
        if stage != failure_stage:
            return True
        return _perform_behavior(failure_mode, stage)

    class FakeDataBaseManager:
        def __init__(self, path):
            self.path = path

        @staticmethod
        def backup_db():
            events.append("backup_db")
            return stage_result("backup_db")

        @staticmethod
        def restore_db():
            events.append("database_restore")
            return _perform_behavior(database_restore, "database_restore")

        def upgrade(self):
            events.append("db_upgrade")
            return stage_result("db_upgrade")

        def update_version(self):
            events.append("version_update")
            return stage_result("version_update")

    class FakeConfigManager:
        def __init__(self, supplied_logger, current_version):
            received_loggers.append(supplied_logger)
            self.current_version = current_version
            self.upgrade_version_list = ["1.0"]

        def update_config_file(self):
            events.append("config_update")
            return stage_result("config_update")

        def restore_config(self):
            events.append("config_restore")
            return _perform_behavior(
                config_restore,
                "config_restore",
                lambda: recovery_files.__setitem__("config", False),
            )

        def delete_backup_file(self):
            events.append("config_backup_cleanup")
            return _perform_behavior(
                config_cleanup,
                "config_backup_cleanup",
                lambda: recovery_files.__setitem__("config", False),
            )

    class FakeExeUpgradeManager:
        def __init__(self, supplied_logger, upgrade_version_list):
            self.logger = supplied_logger
            self.upgrade_version_list = upgrade_version_list

        def run(self):
            events.append("exe_run")
            return stage_result("exe_run")

    def fake_update_version_program(_logger):
        events.append("program_update")
        return stage_result("program_update"), "program update"

    def fake_remove(_path):
        events.append("database_backup_cleanup")
        result = _perform_behavior(
            database_cleanup,
            "database_backup_cleanup",
            lambda: recovery_files.__setitem__("database", False),
        )
        return None if result is True else result

    def fake_set_log_handler(name):
        logger_factory_calls.append(name)
        return logger

    monkeypatch.setattr(upgrade_main, "DataBaseManager", FakeDataBaseManager)
    monkeypatch.setattr(upgrade_main, "ConfigManager", FakeConfigManager)
    monkeypatch.setattr(upgrade_main, "ExeUpgradeManager", FakeExeUpgradeManager)
    monkeypatch.setattr(upgrade_main, "update_version_program", fake_update_version_program)
    monkeypatch.setattr(upgrade_main, "get_current_version", lambda: "0.1")
    monkeypatch.setattr(upgrade_main, "get_version_database_path", lambda: "version.db")
    monkeypatch.setattr(upgrade_main, "DATABASE_PATH", "database.db")
    monkeypatch.setattr(upgrade_main, "BACKUP_DIR", "backup")
    monkeypatch.setattr(upgrade_main.os.path, "exists", lambda _path: recovery_files["database"])
    monkeypatch.setattr(upgrade_main.os, "remove", fake_remove)
    monkeypatch.setattr(
        upgrade_main.LogManager,
        "set_log_handler",
        fake_set_log_handler,
    )

    return SimpleNamespace(
        events=events,
        logger=logger,
        logger_factory_calls=logger_factory_calls,
        received_loggers=received_loggers,
        recovery_files=recovery_files,
    )


def test_run_upgrade_uses_default_logger_and_orders_commit_before_cleanup(monkeypatch):
    scenario = install_fake_upgrade(monkeypatch)

    assert upgrade_main.run_upgrade() is True

    assert scenario.logger_factory_calls == ["core"]
    assert scenario.received_loggers == [scenario.logger]
    assert scenario.events == [
        "backup_db",
        "db_upgrade",
        "config_update",
        "exe_run",
        "program_update",
        "version_update",
        "database_backup_cleanup",
        "config_backup_cleanup",
    ]


@pytest.mark.parametrize(
    ("cleanup_stage", "cleanup_behavior"),
    [
        ("database", "false"),
        ("database", "raise"),
        ("config", "false"),
        ("config", "raise"),
    ],
)
def test_post_commit_cleanup_failures_are_independent_and_non_fatal(
    monkeypatch,
    cleanup_stage,
    cleanup_behavior,
):
    scenario = install_fake_upgrade(
        monkeypatch,
        database_cleanup=(
            cleanup_behavior if cleanup_stage == "database" else "success"
        ),
        config_cleanup=cleanup_behavior if cleanup_stage == "config" else "success",
    )

    assert upgrade_main.run_upgrade(scenario.logger) is True

    assert "version_update" in scenario.events
    assert scenario.events[-2:] == [
        "database_backup_cleanup",
        "config_backup_cleanup",
    ]
    assert "database_restore" not in scenario.events
    assert "config_restore" not in scenario.events
    assert scenario.recovery_files[cleanup_stage] is True
    assert any(level == "warning" for level, _message in scenario.logger.messages)


@pytest.mark.parametrize(
    "failure_stage",
    [
        "db_upgrade",
        "config_update",
        "exe_run",
        "program_update",
        "version_update",
    ],
)
@pytest.mark.parametrize("failure_mode", ["false", "raise"])
def test_pre_commit_stage_failures_stop_the_pipeline_and_restore_both_backups(
    monkeypatch,
    failure_stage,
    failure_mode,
):
    scenario = install_fake_upgrade(
        monkeypatch,
        failure_stage=failure_stage,
        failure_mode=failure_mode,
    )
    stage_order = [
        "backup_db",
        "db_upgrade",
        "config_update",
        "exe_run",
        "program_update",
        "version_update",
    ]
    failure_index = stage_order.index(failure_stage)

    assert upgrade_main.run_upgrade(scenario.logger) is False

    assert scenario.events == stage_order[: failure_index + 1] + [
        "database_restore",
        "config_restore",
    ]
    assert "database_backup_cleanup" not in scenario.events
    assert "config_backup_cleanup" not in scenario.events


@pytest.mark.parametrize("failure_mode", ["false", "raise"])
def test_initial_database_backup_failure_returns_without_rollback(
    monkeypatch,
    failure_mode,
):
    scenario = install_fake_upgrade(
        monkeypatch,
        failure_stage="backup_db",
        failure_mode=failure_mode,
    )

    assert upgrade_main.run_upgrade(scenario.logger) is False

    assert scenario.events == ["backup_db"]
    assert scenario.recovery_files == {"database": True, "config": True}


@pytest.mark.parametrize(
    ("database_restore", "config_restore", "failed_restore"),
    [
        ("false", "success", "database"),
        ("raise", "success", "database"),
        ("success", "false", "config"),
        ("success", "raise", "config"),
    ],
)
def test_restore_failures_are_logged_and_do_not_prevent_the_other_restore(
    monkeypatch,
    database_restore,
    config_restore,
    failed_restore,
):
    scenario = install_fake_upgrade(
        monkeypatch,
        failure_stage="exe_run",
        database_restore=database_restore,
        config_restore=config_restore,
    )

    assert upgrade_main.run_upgrade(scenario.logger) is False

    assert scenario.events[-2:] == ["database_restore", "config_restore"]
    assert scenario.recovery_files[failed_restore] is True
    assert any(level == "error" for level, _message in scenario.logger.messages)
    assert "database_backup_cleanup" not in scenario.events
    assert "config_backup_cleanup" not in scenario.events


def _directory_path(path):
    return str(path) + os.sep


def _configure_real_json_merge_tree(tmp_path, monkeypatch):
    target_dir = tmp_path / "target"
    package_dir = tmp_path / "package"
    config_dir = package_dir / "resources" / "config"
    backup_dir = package_dir / "resources" / "backup"
    ui_config_dir = target_dir / "ui" / "ui_config"
    target_path = ui_config_dir / "target.json"
    source_path = config_dir / "source.json"
    manifest_path = package_dir / "resources" / "version_1.0.yml"
    original_target_bytes = (
        b'{\r\n  "target": "authoritative",\r\n'
        b'  "shared": {"winner": "target"}\r\n}\r\n'
    )

    ui_config_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    target_path.write_bytes(original_target_bytes)
    source_path.write_bytes(
        b'{"added":"from source","shared":{"winner":"source"}}'
    )
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "config": [],
                "json_merge": [
                    {
                        "target": "ui/ui_config/target.json",
                        "source": "resources/config/source.json",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    roots = {
        "ui_pic": target_dir / "ui" / "ui_pic",
        "ui_config": ui_config_dir,
        "ai_model": target_dir / "configs" / "aimodel_config",
        "scanner": target_dir / "configs" / "scanner_barcode_config",
    }
    backups = {
        "ui_pic": backup_dir / "ui_pic",
        "ui_config": backup_dir / "ui_config",
        "ai_model": backup_dir / "ai_model_config",
        "scanner": backup_dir / "scanner_barcode_config",
    }
    monkeypatch.setattr(upgrade_config, "DEFAULT_DIR", _directory_path(package_dir))
    monkeypatch.setattr(
        upgrade_config,
        "DEFAULT_TARGET_DIR",
        _directory_path(target_dir),
    )
    monkeypatch.setattr(upgrade_config, "CONFIG_DIR", _directory_path(config_dir))
    monkeypatch.setattr(upgrade_config, "BACKUP_DIR", _directory_path(backup_dir))
    monkeypatch.setattr(
        upgrade_config,
        "UI_PIC_PATH",
        _directory_path(roots["ui_pic"]),
    )
    monkeypatch.setattr(
        upgrade_config,
        "UI_CONFIG_PATH",
        _directory_path(roots["ui_config"]),
    )
    monkeypatch.setattr(
        upgrade_config,
        "AI_MODEL_CONFIG_PATH",
        _directory_path(roots["ai_model"]),
    )
    monkeypatch.setattr(
        upgrade_config,
        "SCANNER_BARCODE_CONFIG_PATH",
        _directory_path(roots["scanner"]),
    )
    monkeypatch.setattr(
        upgrade_config,
        "BACKUP_UI_PIC_PATH",
        _directory_path(backups["ui_pic"]),
    )
    monkeypatch.setattr(
        upgrade_config,
        "BACKUP_UI_CONFIG_PATH",
        _directory_path(backups["ui_config"]),
    )
    monkeypatch.setattr(
        upgrade_config,
        "BACKUP_AI_MODEL_CONFIG_PATH",
        _directory_path(backups["ai_model"]),
    )
    monkeypatch.setattr(
        upgrade_config,
        "BACKUP_SCANNER_BARCODE_CONFIG_PATH",
        _directory_path(backups["scanner"]),
    )

    return SimpleNamespace(
        target_path=target_path,
        original_target_bytes=original_target_bytes,
        ui_config_backup=backups["ui_config"],
    )


@pytest.mark.parametrize(
    "failure_stage",
    ["exe_run", "program_update", "version_update"],
)
def test_later_failures_restore_exact_json_bytes_with_real_config_manager(
    tmp_path,
    monkeypatch,
    failure_stage,
):
    tree = _configure_real_json_merge_tree(tmp_path, monkeypatch)
    logger = RecordingLogger()
    events = []

    class FakeDataBaseManager:
        def __init__(self, path):
            self.path = path

        @staticmethod
        def backup_db():
            events.append("backup_db")
            return True

        @staticmethod
        def restore_db():
            events.append("database_restore")
            return True

        def upgrade(self):
            events.append("db_upgrade")
            return True

        def update_version(self):
            events.append("version_update")
            assert tree.target_path.read_bytes() != tree.original_target_bytes
            return failure_stage != "version_update"

    class FakeExeUpgradeManager:
        def __init__(self, supplied_logger, upgrade_version_list):
            self.logger = supplied_logger
            self.upgrade_version_list = upgrade_version_list

        def run(self):
            events.append("exe_run")
            assert tree.target_path.read_bytes() != tree.original_target_bytes
            return failure_stage != "exe_run"

    def build_real_config_manager(supplied_logger, current_version):
        manager = upgrade_config.ConfigManager(supplied_logger, current_version)
        manager.upgrade_version_list = ["1.0"]
        return manager

    def fake_program_update(_logger):
        events.append("program_update")
        assert tree.target_path.read_bytes() != tree.original_target_bytes
        return failure_stage != "program_update", "program update"

    monkeypatch.setattr(upgrade_main, "DataBaseManager", FakeDataBaseManager)
    monkeypatch.setattr(upgrade_main, "ConfigManager", build_real_config_manager)
    monkeypatch.setattr(upgrade_main, "ExeUpgradeManager", FakeExeUpgradeManager)
    monkeypatch.setattr(upgrade_main, "update_version_program", fake_program_update)
    monkeypatch.setattr(upgrade_main, "get_current_version", lambda: "99")
    monkeypatch.setattr(upgrade_main, "get_version_database_path", lambda: "version.db")
    monkeypatch.setattr(upgrade_main, "DATABASE_PATH", "database.db")

    assert upgrade_main.run_upgrade(logger) is False

    assert "database_restore" in events
    assert tree.target_path.read_bytes() == tree.original_target_bytes
    assert not tree.ui_config_backup.exists()
