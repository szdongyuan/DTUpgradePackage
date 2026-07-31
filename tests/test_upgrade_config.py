import builtins
import json
import os
import subprocess
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


class FatalReplacementSignal(BaseException):
    pass


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


def track_config_update_mutations(manager, monkeypatch):
    mutation_calls = []
    monkeypatch.setattr(
        manager,
        "backup_config_file",
        lambda paths: mutation_calls.append(("backup", tuple(paths))) or True,
    )
    monkeypatch.setattr(
        manager,
        "smart_remove",
        lambda path: mutation_calls.append(("remove", path)) or True,
    )
    monkeypatch.setattr(
        manager,
        "smart_copy",
        lambda source, target: (
            mutation_calls.append(("copy", source, target)) or True
        ),
    )
    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        lambda *args: mutation_calls.append(("merge", args)) or (True, "ok"),
    )
    return mutation_calls


def install_reporting_probe(
    manager,
    monkeypatch,
    reporter,
    exception_type,
    marker,
):
    report_attempts = []
    if reporter == "print":
        real_print = builtins.print

        def probe_print(*args, **kwargs):
            if args and marker in str(args[0]):
                report_attempts.append(str(args[0]))
                raise exception_type("injected print failure")
            return real_print(*args, **kwargs)

        monkeypatch.setattr(builtins, "print", probe_print)
        return report_attempts

    class ProbingLogger(DummyLogger):
        def _probe(self, level, message):
            if marker in message:
                report_attempts.append(message)
                raise exception_type("injected logger failure")
            self.messages.append((level, message))

        def info(self, message):
            self._probe("info", message)

        def warning(self, message):
            self._probe("warning", message)

        def error(self, message):
            self._probe("error", message)

    manager.logger = ProbingLogger()
    return report_attempts


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


@pytest.mark.parametrize("reporter", ["print", "logger"])
@pytest.mark.parametrize(
    "exception_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_backup_success_reporting_is_best_effort_and_later_roots_are_attempted(
    configured_tree,
    monkeypatch,
    reporter,
    exception_type,
):
    manager = make_manager()
    selected_roots = ("ui_pic", "ai_model_config")
    expected_snapshots = {
        name: snapshot_tree(configured_tree.roots[name])
        for name in selected_roots
    }
    install_reporting_probe(
        manager,
        monkeypatch,
        reporter,
        exception_type,
        "配置文件备份已保存到",
    )

    assert manager.backup_config_file(
        [
            "ui/ui_pic/theme/icon.png",
            "configs/aimodel_config/model.json",
        ]
    ) is True

    assert manager.ui_pic_flag is False
    assert manager.ai_model_config_flag is False
    assert manager.ui_config_flag is True
    assert manager.scannre_barcode_config_flag is True
    assert {
        name: snapshot_tree(configured_tree.backups[name])
        for name in selected_roots
    } == expected_snapshots


@pytest.mark.parametrize("reporter", ["print", "logger"])
@pytest.mark.parametrize(
    "exception_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
@pytest.mark.parametrize(
    "failure_path",
    ["safety", "copy"],
)
def test_backup_failure_reporting_is_best_effort(
    configured_tree,
    monkeypatch,
    reporter,
    exception_type,
    failure_path,
):
    manager = make_manager()
    if failure_path == "safety":
        monkeypatch.setattr(
            manager,
            "_snapshot_ancestor_chain_is_safe",
            lambda *_args: False,
        )
    else:
        monkeypatch.setattr(
            manager,
            "_replace_with_staged_snapshot",
            lambda *_args: False,
        )
    install_reporting_probe(
        manager,
        monkeypatch,
        reporter,
        exception_type,
        "",
    )

    assert manager.backup_config_file(
        ["ui/ui_config/settings.json"]
    ) is False

    assert manager.ui_config_flag is True
    assert not configured_tree.backups["ui_config"].exists()


@pytest.mark.parametrize(
    ("failure_path", "report_marker"),
    [
        ("ancestor_safety", "快照祖先安全检查失败"),
        ("snapshot_copy", "快照暂存复制失败"),
    ],
)
@pytest.mark.parametrize(
    "exception_type",
    [RuntimeError, KeyboardInterrupt, SystemExit],
)
def test_backup_real_snapshot_failure_reporting_is_isolated(
    configured_tree,
    monkeypatch,
    failure_path,
    report_marker,
    exception_type,
):
    manager = make_manager()
    backup_root = configured_tree.backups["ui_config"]

    if failure_path == "ancestor_safety":
        real_path_is_contained = manager._path_is_contained

        def reject_source_ancestor(path, root):
            if (
                os.path.normcase(os.path.normpath(path))
                == os.path.normcase(
                    os.path.normpath(upgrade_config.UI_CONFIG_PATH)
                )
                and os.path.normcase(os.path.normpath(root))
                == os.path.normcase(
                    os.path.normpath(upgrade_config.DEFAULT_TARGET_DIR)
                )
            ):
                return False
            return real_path_is_contained(path, root)

        monkeypatch.setattr(
            manager,
            "_path_is_contained",
            reject_source_ancestor,
        )
    else:
        def partially_fail_copy(_source, destination):
            Path(destination).mkdir(parents=True, exist_ok=True)
            Path(destination).joinpath("partial.bin").write_bytes(
                b"partial snapshot"
            )
            return False

        monkeypatch.setattr(manager, "smart_copy", partially_fail_copy)

    report_attempts = install_reporting_probe(
        manager,
        monkeypatch,
        "logger",
        exception_type,
        report_marker,
    )

    assert manager.backup_config_file(
        ["ui/ui_config/settings.json"]
    ) is False

    assert report_attempts
    assert manager.ui_config_flag is True
    assert manager.ui_pic_flag is True
    assert manager.ai_model_config_flag is True
    assert manager.scannre_barcode_config_flag is True
    assert not backup_root.exists()
    assert (
        not configured_tree.backup_dir.exists()
        or swap_artifacts(backup_root) == []
    )


def test_backup_operational_base_exception_propagates_and_cleans_staging(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    backup_root = configured_tree.backups["ui_config"]

    def abort_partial_copy(_source, destination):
        Path(destination).mkdir(parents=True, exist_ok=True)
        Path(destination).joinpath("partial.bin").write_bytes(
            b"partial snapshot"
        )
        raise FatalReplacementSignal("injected operational failure")

    monkeypatch.setattr(manager, "smart_copy", abort_partial_copy)

    with pytest.raises(
        FatalReplacementSignal,
        match="injected operational failure",
    ):
        manager.backup_config_file(["ui/ui_config/settings.json"])

    assert manager.ui_config_flag is True
    assert manager.ui_pic_flag is True
    assert manager.ai_model_config_flag is True
    assert manager.scannre_barcode_config_flag is True
    assert not backup_root.exists()
    assert swap_artifacts(backup_root) == []


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
    real_rename = os.rename

    def fail_existing_destination_move(source, destination):
        if (
            Path(source) == backup_root
            and Path(destination).name.startswith(f".{backup_root.name}.old-")
        ):
            raise OSError("injected existing destination move failure")
        return real_rename(source, destination)

    monkeypatch.setattr(upgrade_config.os, "rename", fail_existing_destination_move)

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
    real_rename = os.rename
    replace_calls = []

    def fail_staging_install(source, destination):
        replace_calls.append((Path(source), Path(destination)))
        if (
            Path(destination) == target_root
            and Path(source).name.startswith(f".{target_root.name}.staging-")
        ):
            raise OSError("injected staging install failure")
        return real_rename(source, destination)

    monkeypatch.setattr(upgrade_config.os, "rename", fail_staging_install)

    assert manager.restore_config() is False

    assert len(replace_calls) == 3
    assert snapshot_tree(target_root) == expected_target
    assert snapshot_tree(backup_root) == expected_backup
    assert swap_artifacts(target_root) == []


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize(
    "interruption_transition",
    [
        "after_destination_move",
        "after_staging_install",
        "before_rollback_restore",
        "after_rollback_restore",
    ],
)
def test_staged_snapshot_swap_interruption_preserves_exact_recovery_data(
    tmp_path,
    monkeypatch,
    interruption_type,
    interruption_transition,
):
    manager = make_manager()
    source = tmp_path / "snapshot-source"
    destination = tmp_path / "live-destination"
    source.joinpath("nested").mkdir(parents=True)
    destination.joinpath("nested").mkdir(parents=True)
    source.joinpath("snapshot.bin").write_bytes(b"\x00snapshot-data\xff")
    source.joinpath("nested", "state.txt").write_bytes(b"snapshot state")
    destination.joinpath("live.bin").write_bytes(b"\x00live-data\xff")
    destination.joinpath("nested", "state.txt").write_bytes(b"live state")
    expected_source = snapshot_tree(source)
    expected_destination = snapshot_tree(destination)
    real_rename = os.rename
    staging_install_failed = False

    def interrupt_replace(source_path, destination_path):
        nonlocal staging_install_failed
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        is_destination_move = (
            source_path == destination
            and destination_path.name.startswith(
                f".{destination.name}.old-"
            )
        )
        is_staging_install = (
            destination_path == destination
            and source_path.name.startswith(
                f".{destination.name}.staging-"
            )
        )
        is_rollback_restore = (
            destination_path == destination
            and source_path.name.startswith(
                f".{destination.name}.old-"
            )
        )

        if (
            interruption_transition
            in {"before_rollback_restore", "after_rollback_restore"}
            and is_staging_install
            and not staging_install_failed
        ):
            staging_install_failed = True
            raise OSError("injected staging installation failure")
        if (
            interruption_transition == "before_rollback_restore"
            and is_rollback_restore
        ):
            raise interruption_type("injected rollback interruption")

        result = real_rename(source_path, destination_path)
        if (
            interruption_transition == "after_destination_move"
            and is_destination_move
        ):
            raise interruption_type("injected destination move interruption")
        if (
            interruption_transition == "after_staging_install"
            and is_staging_install
        ):
            raise interruption_type("injected staging install interruption")
        if (
            interruption_transition == "after_rollback_restore"
            and is_rollback_restore
        ):
            raise interruption_type("injected rollback restore interruption")
        return result

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        interrupt_replace,
    )

    with pytest.raises(interruption_type):
        manager._replace_with_staged_snapshot(source, destination)

    old_artifacts = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(f".{destination.name}.old-")
    ]
    destination_tree = (
        snapshot_tree(destination) if destination.exists() else None
    )
    recovery_trees = [snapshot_tree(path) for path in old_artifacts]

    if interruption_transition == "after_staging_install":
        assert (
            destination_tree == expected_source
            or expected_destination in recovery_trees
        )
    else:
        assert (
            destination_tree == expected_destination
            or expected_destination in recovery_trees
        )
    if not destination.exists():
        assert expected_destination in recovery_trees


def test_staged_snapshot_indeterminate_recovery_preserves_all_artifacts(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("source.bin").write_bytes(b"replacement snapshot")
    destination = tmp_path / "destination"
    destination.mkdir()
    destination.joinpath("target.bin").write_bytes(b"original snapshot")
    manager = make_manager()
    real_rename = os.rename
    real_lexists = manager._snapshot_path_lexists
    install_failed = {"value": False}

    def fail_install(source_path, destination_path):
        if (
            Path(destination_path) == destination
            and Path(source_path).name.startswith(
                f".{destination.name}.staging-"
            )
        ):
            install_failed["value"] = True
            raise OSError("injected snapshot install failure")
        return real_rename(source_path, destination_path)

    def indeterminate_old(path):
        if (
            install_failed["value"]
            and path is not None
            and Path(path).name.startswith(
                f".{destination.name}.old-"
            )
        ):
            return None
        return real_lexists(path)

    monkeypatch.setattr(upgrade_config.os, "rename", fail_install)
    monkeypatch.setattr(
        manager,
        "_snapshot_path_lexists",
        indeterminate_old,
    )

    assert manager._replace_with_staged_snapshot(
        str(source),
        str(destination),
        source_anchor=str(tmp_path),
        destination_anchor=str(tmp_path),
    ) is False

    artifacts = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(
            (
                f".{destination.name}.old-",
                f".{destination.name}.staging-",
            )
        )
    ]
    assert not destination.exists()
    assert len(artifacts) == 2
    artifact_trees = [snapshot_tree(path) for path in artifacts]
    assert {"target.bin": b"original snapshot"} in artifact_trees
    assert {"source.bin": b"replacement snapshot"} in artifact_trees


def test_staged_snapshot_absent_destination_install_is_atomic_no_clobber(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("source.bin").write_bytes(b"replacement snapshot")
    destination = tmp_path / "destination"
    manager = make_manager()
    real_rename = os.rename
    race_triggered = {"value": False}

    def create_destination_immediately_before_install(
        source_path,
        destination_path,
    ):
        if (
            Path(destination_path) == destination
            and Path(source_path).name.startswith(
                f".{destination.name}.staging-"
            )
            and not race_triggered["value"]
        ):
            destination.mkdir()
            destination.joinpath("concurrent.bin").write_bytes(
                b"concurrent destination"
            )
            race_triggered["value"] = True
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        create_destination_immediately_before_install,
    )

    assert manager._replace_with_staged_snapshot(
        str(source),
        str(destination),
        source_anchor=str(tmp_path),
        destination_anchor=str(tmp_path),
    ) is False
    assert race_triggered["value"] is True
    assert snapshot_tree(destination) == {
        "concurrent.bin": b"concurrent destination"
    }


def test_staged_snapshot_destination_move_verifies_exact_moved_identity(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("source.bin").write_bytes(b"replacement snapshot")
    destination = tmp_path / "destination"
    destination.mkdir()
    destination.joinpath("original.bin").write_bytes(
        b"original recovery data"
    )
    displaced_original = tmp_path / "displaced-original"
    manager = make_manager()
    real_rename = os.rename
    race_triggered = {"value": False}

    def replace_destination_immediately_before_move(
        source_path,
        destination_path,
    ):
        if (
            Path(source_path) == destination
            and Path(destination_path).name.startswith(
                f".{destination.name}.old-"
            )
            and not race_triggered["value"]
        ):
            real_rename(destination, displaced_original)
            destination.mkdir()
            destination.joinpath("concurrent.bin").write_bytes(
                b"concurrent replacement"
            )
            race_triggered["value"] = True
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        replace_destination_immediately_before_move,
    )

    assert manager._replace_with_staged_snapshot(
        str(source),
        str(destination),
        source_anchor=str(tmp_path),
        destination_anchor=str(tmp_path),
    ) is False
    assert race_triggered["value"] is True
    assert snapshot_tree(destination) == {
        "concurrent.bin": b"concurrent replacement"
    }
    assert snapshot_tree(displaced_original) == {
        "original.bin": b"original recovery data"
    }


def test_staged_snapshot_existing_destination_install_is_atomic_no_clobber(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("source.bin").write_bytes(b"replacement snapshot")
    destination = tmp_path / "destination"
    destination.mkdir()
    destination.joinpath("original.bin").write_bytes(
        b"original recovery data"
    )
    manager = make_manager()
    real_rename = os.rename
    race_triggered = {"value": False}

    def create_destination_immediately_before_install(
        source_path,
        destination_path,
    ):
        if (
            Path(destination_path) == destination
            and Path(source_path).name.startswith(
                f".{destination.name}.staging-"
            )
            and not race_triggered["value"]
        ):
            destination.mkdir()
            destination.joinpath("concurrent.bin").write_bytes(
                b"concurrent destination"
            )
            race_triggered["value"] = True
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        create_destination_immediately_before_install,
    )

    assert manager._replace_with_staged_snapshot(
        str(source),
        str(destination),
        source_anchor=str(tmp_path),
        destination_anchor=str(tmp_path),
    ) is False
    assert race_triggered["value"] is True
    assert snapshot_tree(destination) == {
        "concurrent.bin": b"concurrent destination"
    }
    recovery_artifacts = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(f".{destination.name}.old-")
    ]
    assert len(recovery_artifacts) == 1
    assert snapshot_tree(recovery_artifacts[0]) == {
        "original.bin": b"original recovery data"
    }


def test_staged_snapshot_recovery_is_atomic_no_clobber(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("source.bin").write_bytes(b"replacement snapshot")
    destination = tmp_path / "destination"
    destination.mkdir()
    destination.joinpath("original.bin").write_bytes(
        b"original recovery data"
    )
    manager = make_manager()
    real_rename = os.rename
    race_triggered = {"value": False}

    def fail_install_then_create_destination_before_recovery(
        source_path,
        destination_path,
    ):
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if (
            destination_path == destination
            and source_path.name.startswith(
                f".{destination.name}.staging-"
            )
        ):
            raise OSError("injected staging install failure")
        if (
            destination_path == destination
            and source_path.name.startswith(f".{destination.name}.old-")
            and not race_triggered["value"]
        ):
            destination.mkdir()
            destination.joinpath("concurrent.bin").write_bytes(
                b"concurrent destination"
            )
            race_triggered["value"] = True
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        fail_install_then_create_destination_before_recovery,
    )

    assert manager._replace_with_staged_snapshot(
        str(source),
        str(destination),
        source_anchor=str(tmp_path),
        destination_anchor=str(tmp_path),
    ) is False
    assert race_triggered["value"] is True
    assert snapshot_tree(destination) == {
        "concurrent.bin": b"concurrent destination"
    }
    recovery_artifacts = [
        path
        for path in tmp_path.iterdir()
        if path.name.startswith(f".{destination.name}.old-")
    ]
    assert len(recovery_artifacts) == 1
    assert snapshot_tree(recovery_artifacts[0]) == {
        "original.bin": b"original recovery data"
    }


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


@pytest.mark.parametrize("failing_reporter", ["print", "logger"])
def test_restore_reporting_failure_does_not_stop_later_selected_roots(
    configured_tree,
    monkeypatch,
    failing_reporter,
):
    manager = make_manager()
    manager.ui_pic_flag = False
    manager.ai_model_config_flag = False
    restore_calls = []
    cleanup_calls = []
    report_failure = {"raised": False}

    monkeypatch.setattr(
        manager,
        "_replace_with_staged_snapshot",
        lambda source, destination, **_kwargs: (
            restore_calls.append((source, destination)) or True
        ),
    )
    monkeypatch.setattr(
        manager,
        "delete_backup_file",
        lambda: cleanup_calls.append(True) or True,
    )

    if failing_reporter == "print":
        real_print = print

        def fail_first_root_print(*args, **kwargs):
            message = " ".join(str(arg) for arg in args)
            if (
                "ui图片配置文件已恢复" in message
                and not report_failure["raised"]
            ):
                report_failure["raised"] = True
                raise RuntimeError("injected restore print failure")
            return real_print(*args, **kwargs)

        monkeypatch.setattr("builtins.print", fail_first_root_print)
    else:
        real_info = manager.logger.info

        def fail_first_root_log(message):
            if (
                "ui图片配置文件已恢复" in message
                and not report_failure["raised"]
            ):
                report_failure["raised"] = True
                raise RuntimeError("injected restore logger failure")
            return real_info(message)

        monkeypatch.setattr(manager.logger, "info", fail_first_root_log)

    assert manager.restore_config() is True

    assert report_failure["raised"] is True
    assert restore_calls == [
        (
            upgrade_config.BACKUP_UI_PIC_PATH,
            upgrade_config.UI_PIC_PATH,
        ),
        (
            upgrade_config.BACKUP_AI_MODEL_CONFIG_PATH,
            upgrade_config.AI_MODEL_CONFIG_PATH,
        ),
    ]
    assert cleanup_calls == [True]


@pytest.mark.parametrize("failing_reporter", ["print", "logger"])
def test_delete_reporting_failure_does_not_stop_later_selected_roots(
    configured_tree,
    monkeypatch,
    failing_reporter,
):
    manager = make_manager()
    manager.ui_pic_flag = False
    manager.ai_model_config_flag = False
    remove_calls = []
    report_failure = {"raised": False}
    monkeypatch.setattr(
        manager,
        "smart_remove",
        lambda path: remove_calls.append(path) or True,
    )

    if failing_reporter == "print":
        real_print = print

        def fail_first_root_print(*args, **kwargs):
            message = " ".join(str(arg) for arg in args)
            if (
                "ui图片配置备份文件已删除" in message
                and not report_failure["raised"]
            ):
                report_failure["raised"] = True
                raise RuntimeError("injected delete print failure")
            return real_print(*args, **kwargs)

        monkeypatch.setattr("builtins.print", fail_first_root_print)
    else:
        real_info = manager.logger.info

        def fail_first_root_log(message):
            if (
                "ui图片配置备份文件已删除" in message
                and not report_failure["raised"]
            ):
                report_failure["raised"] = True
                raise RuntimeError("injected delete logger failure")
            return real_info(message)

        monkeypatch.setattr(manager.logger, "info", fail_first_root_log)

    assert manager.delete_backup_file() is True

    assert report_failure["raised"] is True
    assert remove_calls == [
        upgrade_config.BACKUP_UI_PIC_PATH,
        upgrade_config.BACKUP_AI_MODEL_CONFIG_PATH,
    ]


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


@pytest.mark.parametrize(
    ("relative_path", "root_name"),
    [
        ("ui/ui_pic", "ui_pic"),
        ("ui/ui_config", "ui_config"),
        ("configs/aimodel_config", "ai_model_config"),
        ("configs/scanner_barcode_config", "scanner_barcode_config"),
    ],
)
def test_ordinary_config_destination_accepts_managed_root_itself(
    configured_tree,
    relative_path,
    root_name,
):
    destination_path, error = make_manager()._resolve_ordinary_config_destination(
        relative_path
    )

    assert error is None
    assert os.path.normcase(os.path.normpath(destination_path)) == os.path.normcase(
        os.path.normpath(configured_tree.roots[root_name])
    )


@pytest.mark.parametrize(
    "config_entry",
    [
        "ui/ui_pic",
        r"ui\ui_pic",
        "ui/ui_pic/",
        "ui/ui_pic/sub/..",
    ],
    ids=[
        "canonical",
        "backslash_alias",
        "trailing_separator_alias",
        "normalized_parent_alias",
    ],
)
def test_ordinary_config_managed_root_entry_is_backed_up_and_replaced(
    configured_tree,
    config_entry,
):
    source_root = configured_tree.config_dir / "ui_pic"
    source_root.mkdir()
    (source_root / "marker.txt").write_text(
        "replacement ui_pic",
        encoding="utf-8",
    )
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [config_entry],
                    "json_merge": [],
                },
            )
        ],
    )

    assert manager.update_config_file() is True

    assert (
        configured_tree.backups["ui_pic"] / "marker.txt"
    ).read_text(encoding="utf-8") == "original ui_pic"
    assert (
        configured_tree.roots["ui_pic"] / "marker.txt"
    ).read_text(encoding="utf-8") == "replacement ui_pic"
    assert manager.ui_pic_flag is False
    assert not any(
        "ordinary_config error: destination_path:symlink_escape" in message
        for message in logged_messages(manager, "error")
    )


def test_missing_ordinary_source_fails_before_backup_or_target_mutation(
    configured_tree,
    monkeypatch,
):
    target_root = configured_tree.roots["ui_pic"]
    original_target_tree = snapshot_tree(target_root)
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_pic"],
                    "json_merge": [],
                },
            )
        ],
    )
    events = []
    real_backup = manager.backup_config_file
    real_remove = manager.smart_remove
    real_copy = manager.smart_copy

    def track_backup(paths):
        events.append("backup")
        return real_backup(paths)

    def track_remove(path):
        events.append("remove")
        return real_remove(path)

    def track_copy(source, target):
        events.append("copy")
        return real_copy(source, target)

    monkeypatch.setattr(manager, "backup_config_file", track_backup)
    monkeypatch.setattr(manager, "smart_remove", track_remove)
    monkeypatch.setattr(manager, "smart_copy", track_copy)

    assert manager.update_config_file() is False

    assert events == []
    assert snapshot_tree(target_root) == original_target_tree
    assert manager.ui_pic_flag is True
    assert not configured_tree.backups["ui_pic"].exists()
    assert any(
        "ordinary_config error: source_path:missing_path" in message
        for message in logged_messages(manager, "error")
    )


def test_missing_ordinary_source_returns_candidate_and_missing_path(
    configured_tree,
):
    manager = make_manager()
    destination = configured_tree.roots["ui_pic"]
    expected_candidate = configured_tree.config_dir / "ui_pic"

    source_path, error = manager._resolve_ordinary_config_source(destination)

    assert source_path == str(expected_candidate)
    assert error == "missing_path"


@pytest.mark.parametrize("missing_parent", ["resources", "config"])
def test_ordinary_source_missing_parent_is_fatal(
    configured_tree,
    missing_parent,
):
    manager = make_manager()
    configured_tree.config_dir.rmdir()
    if missing_parent == "resources":
        configured_tree.config_dir.parent.rmdir()

    source_path, error = manager._resolve_ordinary_config_source(
        configured_tree.roots["ui_pic"]
    )

    assert source_path is None
    assert error == "missing_parent"


def test_dangling_ordinary_source_is_not_missing(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    source_path = configured_tree.config_dir / "ui_pic"
    real_lexists = os.path.lexists
    real_lstat = os.lstat
    real_stat = os.stat

    def dangling_lexists(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        ):
            return True
        return real_lexists(path)

    def dangling_lstat(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        ):
            return SimpleNamespace(
                st_mode=upgrade_config.stat.S_IFLNK,
                st_file_attributes=0,
            )
        return real_lstat(path, *args, **kwargs)

    def dangling_stat(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        ):
            raise FileNotFoundError("dangling source")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(upgrade_config.os.path, "lexists", dangling_lexists)
    monkeypatch.setattr(upgrade_config.os, "lstat", dangling_lstat)
    monkeypatch.setattr(upgrade_config.os, "stat", dangling_stat)

    resolved_source, error = manager._resolve_ordinary_config_source(
        configured_tree.roots["ui_pic"]
    )

    assert resolved_source is None
    assert error == "dangling_link"


def test_reparse_ordinary_source_is_not_missing(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    source_path = configured_tree.config_dir / "ui_pic"
    real_lexists = os.path.lexists
    real_lstat = os.lstat

    def reparse_lexists(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        ):
            return True
        return real_lexists(path)

    def reparse_lstat(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        ):
            return SimpleNamespace(
                st_mode=upgrade_config.stat.S_IFREG,
                st_file_attributes=getattr(
                    upgrade_config.stat,
                    "FILE_ATTRIBUTE_REPARSE_POINT",
                    0x400,
                ),
            )
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(upgrade_config.os.path, "lexists", reparse_lexists)
    monkeypatch.setattr(upgrade_config.os, "lstat", reparse_lstat)

    resolved_source, error = manager._resolve_ordinary_config_source(
        configured_tree.roots["ui_pic"]
    )

    assert resolved_source is None
    assert error == "unsupported_type"


def test_ordinary_source_inconsistent_absence_is_fatal(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    source_path = configured_tree.config_dir / "ui_pic"
    source_path.write_text("replacement", encoding="utf-8")
    real_lexists = os.path.lexists

    def inconsistent_lexists(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        ):
            return False
        return real_lexists(path)

    monkeypatch.setattr(upgrade_config.os.path, "lexists", inconsistent_lexists)

    resolved_source, error = manager._resolve_ordinary_config_source(
        configured_tree.roots["ui_pic"]
    )

    assert resolved_source is None
    assert error == "identity_check_failed"


def test_ordinary_source_permission_failure_is_fatal(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    source_path = configured_tree.config_dir / "ui_pic"
    source_path.write_text("replacement", encoding="utf-8")
    real_lstat = os.lstat

    def permission_denied_lstat(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        ):
            raise PermissionError("source identity denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(upgrade_config.os, "lstat", permission_denied_lstat)

    resolved_source, error = manager._resolve_ordinary_config_source(
        configured_tree.roots["ui_pic"]
    )

    assert resolved_source is None
    assert error == "identity_check_failed"


def test_ordinary_source_unsupported_type_is_fatal(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    source_path = configured_tree.config_dir / "ui_pic"
    real_lexists = os.path.lexists
    real_lstat = os.lstat

    def source_lexists(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        ):
            return True
        return real_lexists(path)

    def unsupported_lstat(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(source_path)
        ):
            return SimpleNamespace(st_mode=0, st_file_attributes=0)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(upgrade_config.os.path, "lexists", source_lexists)
    monkeypatch.setattr(upgrade_config.os, "lstat", unsupported_lstat)

    resolved_source, error = manager._resolve_ordinary_config_source(
        configured_tree.roots["ui_pic"]
    )

    assert resolved_source is None
    assert error == "unsupported_type"


def test_ordinary_source_invalid_destination_is_fatal():
    manager = make_manager()

    source_path, error = manager._resolve_ordinary_config_source(None)

    assert source_path is None
    assert error == "invalid_path"


@pytest.mark.parametrize(
    "source_error",
    [
        "dangling_link",
        "identity_check_failed",
        "unsupported_type",
        "invalid_path",
        "symlink_escape",
        "missing_parent",
    ],
)
def test_ordinary_source_non_missing_errors_fail_before_backup_or_mutation(
    configured_tree,
    monkeypatch,
    source_error,
):
    target_root = configured_tree.roots["ui_pic"]
    original_target_tree = snapshot_tree(target_root)
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_pic"],
                    "json_merge": [],
                },
            )
        ],
    )
    events = []
    monkeypatch.setattr(
        manager,
        "_resolve_ordinary_config_source",
        lambda _destination: (None, source_error),
    )
    monkeypatch.setattr(
        manager,
        "backup_config_file",
        lambda _paths: events.append("backup") or True,
    )
    monkeypatch.setattr(
        manager,
        "_replace_with_staged_ordinary_config",
        lambda _source, _destination: events.append("replace") or True,
    )

    assert manager.update_config_file() is False

    assert events == []
    assert snapshot_tree(target_root) == original_target_tree
    assert manager.ui_pic_flag is True
    assert not configured_tree.backups["ui_pic"].exists()
    assert any(
        f"ordinary_config error: source_path:{source_error}" in message
        for message in logged_messages(manager, "error")
    )


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

    def track_replace(source, target):
        events.append(("replace", source, target))
        return True

    def track_merge(target, source):
        events.append(("merge", target, source))
        return True, "ok"

    monkeypatch.setattr(manager, "backup_config_file", track_backup)
    monkeypatch.setattr(
        manager,
        "_replace_with_staged_ordinary_config",
        track_replace,
    )
    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        track_merge,
        raising=False,
    )

    assert manager.update_config_file() is True

    event_names = [event[0] for event in events]
    assert event_names == ["backup", "replace", "backup", "merge"]
    assert events[0][1] == (replacement_relative,)
    expected_replacement_target = os.path.abspath(
        os.path.join(upgrade_config.DEFAULT_TARGET_DIR, replacement_relative)
    )
    assert events[1][2] == expected_replacement_target
    assert events[2][1] == (merge_relative,)


def test_ordinary_config_swap_failure_stops_before_merges_and_keeps_target(
    configured_tree,
    monkeypatch,
):
    config_relative = "ui/ui_config/ordinary.json"
    merge_relative = "ui/ui_config/merged.json"
    target = write_json(
        configured_tree.roots["ui_config"] / "ordinary.json",
        {"state": "original"},
    )
    source = write_json(
        configured_tree.config_dir / "ordinary.json",
        {"state": "replacement"},
    )
    write_json(
        configured_tree.roots["ui_config"] / "merged.json",
        {"target": True},
    )
    write_json(configured_tree.config_dir / "patch.json", {"source": True})
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [config_relative],
                    "json_merge": [
                        {
                            "target": merge_relative,
                            "source": "resources/config/patch.json",
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
                            "target": merge_relative,
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            ),
        ],
    )
    original_target_bytes = target.read_bytes()
    original_source_bytes = source.read_bytes()
    real_rename = os.rename
    real_apply_json_merges = manager._apply_json_merges
    phase_calls = []
    helper_calls = []

    def fail_target_swap(source_path, destination_path):
        if (
            Path(source_path) == target
            and Path(destination_path).name.startswith(
                f".{target.name}.old-"
            )
        ):
            raise OSError("injected ordinary target swap failure")
        return real_rename(source_path, destination_path)

    def track_phase(entries):
        phase_calls.append(entries)
        return real_apply_json_merges(entries)

    def track_helper(*args):
        helper_calls.append(args)
        return True, "ok"

    monkeypatch.setattr(upgrade_config.os, "rename", fail_target_swap)
    monkeypatch.setattr(manager, "_apply_json_merges", track_phase)
    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        track_helper,
    )

    assert manager.update_config_file() is False

    assert phase_calls == []
    assert helper_calls == []
    assert target.read_bytes() == original_target_bytes
    assert source.read_bytes() == original_source_bytes
    assert (
        configured_tree.backups["ui_config"] / "ordinary.json"
    ).read_bytes() == original_target_bytes
    assert swap_artifacts(target) == []
    assert any(
        "配置文件升级失败" in message
        for message in logged_messages(manager, "error")
    )


def test_ordinary_config_copy_failure_stops_before_merges_and_can_restore(
    configured_tree,
    monkeypatch,
):
    config_relative = "ui/ui_config/ordinary.json"
    merge_relative = "ui/ui_config/merged.json"
    target = write_json(
        configured_tree.roots["ui_config"] / "ordinary.json",
        {"state": "original"},
    )
    source = write_json(
        configured_tree.config_dir / "ordinary.json",
        {"state": "replacement"},
    )
    write_json(
        configured_tree.roots["ui_config"] / "merged.json",
        {"target": True},
    )
    write_json(configured_tree.config_dir / "patch.json", {"source": True})
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [config_relative],
                    "json_merge": [
                        {
                            "target": merge_relative,
                            "source": "resources/config/patch.json",
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
                            "target": merge_relative,
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            ),
        ],
    )
    original_target_bytes = target.read_bytes()
    original_source_bytes = source.read_bytes()
    real_smart_copy = manager.smart_copy
    real_apply_json_merges = manager._apply_json_merges
    phase_calls = []
    helper_calls = []

    def fail_ordinary_copy(source_path, target_path):
        if (
            Path(source_path) == source
            and Path(target_path).parent == target.parent
            and Path(target_path).name.startswith(
                f".{target.name}.staging-"
            )
        ):
            return False
        return real_smart_copy(source_path, target_path)

    def track_phase(entries):
        phase_calls.append(entries)
        return real_apply_json_merges(entries)

    def track_helper(*args):
        helper_calls.append(args)
        return True, "ok"

    monkeypatch.setattr(manager, "smart_copy", fail_ordinary_copy)
    monkeypatch.setattr(manager, "_apply_json_merges", track_phase)
    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        track_helper,
    )

    assert manager.update_config_file() is False

    assert phase_calls == []
    assert helper_calls == []
    assert target.read_bytes() == original_target_bytes
    assert source.read_bytes() == original_source_bytes
    assert (
        configured_tree.backups["ui_config"] / "ordinary.json"
    ).read_bytes() == original_target_bytes
    assert swap_artifacts(target) == []
    assert any(
        "配置文件升级失败" in message
        for message in logged_messages(manager, "error")
    )

    assert manager.restore_config() is True
    assert target.read_bytes() == original_target_bytes


def test_ordinary_config_old_artifact_cleanup_failure_rolls_back_target(
    configured_tree,
    monkeypatch,
):
    target = configured_tree.roots["ui_config"] / "cleanup-race.bin"
    source = configured_tree.config_dir / "cleanup-race.bin"
    target.write_bytes(b"\x00original-cleanup-target\xff")
    source.write_bytes(b"\x00replacement-cleanup-source\xff")
    original_target_bytes = target.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_config/cleanup-race.bin"],
                    "json_merge": [],
                },
            )
        ],
    )
    real_cleanup = manager._cleanup_snapshot_artifact
    cleanup_failure_injected = {"value": False}

    def fail_old_artifact_cleanup(path):
        if (
            path
            and Path(path).name.startswith(f".{target.name}.old-")
            and not cleanup_failure_injected["value"]
        ):
            cleanup_failure_injected["value"] = True
            return False
        return real_cleanup(path)

    monkeypatch.setattr(
        manager,
        "_cleanup_snapshot_artifact",
        fail_old_artifact_cleanup,
    )

    assert manager.update_config_file() is False

    assert cleanup_failure_injected["value"] is True
    assert target.is_file()
    assert target.read_bytes() == original_target_bytes
    assert swap_artifacts(target) == []


@pytest.mark.parametrize(
    ("interruption_type", "expected_code"),
    [
        (KeyboardInterrupt, None),
        (SystemExit, 23),
        (FatalReplacementSignal, None),
    ],
)
def test_ordinary_config_swap_interruption_reraises_after_restoring_target(
    configured_tree,
    monkeypatch,
    interruption_type,
    expected_code,
):
    target = configured_tree.roots["ui_config"] / "interrupted.bin"
    source = configured_tree.config_dir / "interrupted.bin"
    target.write_bytes(b"\x00original-interrupted-target\xff")
    source.write_bytes(b"\x00replacement-interrupted-source\xff")
    original_target_bytes = target.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_config/interrupted.bin"],
                    "json_merge": [],
                },
            )
        ],
    )
    real_rename = os.rename
    interruption_injected = {"value": False}

    def interrupt_after_staging_install(source_path, destination_path):
        result = real_rename(source_path, destination_path)
        if (
            Path(destination_path) == target
            and Path(source_path).name.startswith(
                f".{target.name}.staging-"
            )
            and not interruption_injected["value"]
        ):
            interruption_injected["value"] = True
            if interruption_type is SystemExit:
                raise interruption_type(expected_code)
            raise interruption_type("injected ordinary swap interruption")
        return result

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        interrupt_after_staging_install,
    )

    with pytest.raises(interruption_type) as raised:
        manager.update_config_file()

    assert interruption_injected["value"] is True
    if interruption_type is SystemExit:
        assert raised.value.code == expected_code
    assert target.is_file()
    assert target.read_bytes() == original_target_bytes
    assert swap_artifacts(target) == []


def test_ordinary_config_failed_restoration_keeps_explicit_recovery_artifact(
    configured_tree,
    monkeypatch,
):
    target = configured_tree.roots["ui_config"] / "recovery.bin"
    source = configured_tree.config_dir / "recovery.bin"
    target.write_bytes(b"\x00original-recovery-target\xff")
    source.write_bytes(b"\x00replacement-recovery-source\xff")
    original_target_bytes = target.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_config/recovery.bin"],
                    "json_merge": [],
                },
            )
        ],
    )
    real_rename = os.rename

    def fail_install_and_restoration(source_path, destination_path):
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if (
            destination_path == target
            and source_path.name.startswith(f".{target.name}.staging-")
        ):
            raise OSError("injected staging install failure")
        if (
            destination_path == target
            and source_path.name.startswith(f".{target.name}.old-")
        ):
            raise OSError("injected original restoration failure")
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        fail_install_and_restoration,
    )

    assert manager.update_config_file() is False

    recovery_artifacts = [
        path
        for path in target.parent.iterdir()
        if path.name.startswith(f".{target.name}.old-")
    ]
    assert not target.exists()
    assert len(recovery_artifacts) == 1
    assert recovery_artifacts[0].read_bytes() == original_target_bytes
    assert not any(
        path.name.startswith(f".{target.name}.staging-")
        for path in target.parent.iterdir()
    )


def test_ordinary_config_logger_interruption_is_best_effort_during_copy_failure(
    configured_tree,
    monkeypatch,
):
    target = configured_tree.roots["ui_config"] / "logger.bin"
    source = configured_tree.config_dir / "logger.bin"
    target.write_bytes(b"original logger target")
    source.write_bytes(b"replacement logger source")
    original_target = target.read_bytes()
    manager = make_manager()

    class InterruptingLogger(DummyLogger):
        def error(self, message):
            raise KeyboardInterrupt("logger must not control replacement")

    manager.logger = InterruptingLogger()
    monkeypatch.setattr(manager, "smart_copy", lambda *_args: False)

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False
    assert target.read_bytes() == original_target
    assert swap_artifacts(target) == []


@pytest.mark.parametrize("reporter", ["print", "logger"])
@pytest.mark.parametrize(
    "exception_type",
    [RuntimeError, KeyboardInterrupt],
)
@pytest.mark.parametrize(
    ("report_phase", "marker"),
    [
        ("before_replacement", "更新配置文件:"),
        ("after_replacement", "配置文件升级成功"),
        ("replacement_failure", "配置文件升级失败"),
    ],
)
def test_ordinary_update_reporting_is_best_effort_at_transaction_boundaries(
    configured_tree,
    monkeypatch,
    reporter,
    exception_type,
    report_phase,
    marker,
):
    config_relative = "ui/ui_config/reporting-boundary.bin"
    target = configured_tree.roots["ui_config"] / "reporting-boundary.bin"
    source = configured_tree.config_dir / "reporting-boundary.bin"
    target.write_bytes(b"original target")
    source.write_bytes(b"replacement source")
    original_target = target.read_bytes()
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [config_relative],
                    "json_merge": [],
                },
            )
        ],
    )
    replacement_calls = []
    real_replacement = manager._replace_with_staged_ordinary_config

    def replacement(source_path, destination_path):
        replacement_calls.append((source_path, destination_path))
        if report_phase == "replacement_failure":
            return False
        return real_replacement(source_path, destination_path)

    monkeypatch.setattr(
        manager,
        "_replace_with_staged_ordinary_config",
        replacement,
    )
    install_reporting_probe(
        manager,
        monkeypatch,
        reporter,
        exception_type,
        marker,
    )

    result = manager.update_config_file()

    assert len(replacement_calls) == 1
    if report_phase == "replacement_failure":
        assert result is False
        assert target.read_bytes() == original_target
    else:
        assert result is True
        assert target.read_bytes() == b"replacement source"


@pytest.mark.parametrize("reporter", ["print", "logger"])
@pytest.mark.parametrize(
    "exception_type",
    [RuntimeError, KeyboardInterrupt],
)
def test_ordinary_update_outer_exception_reporting_is_best_effort(
    configured_tree,
    monkeypatch,
    reporter,
    exception_type,
):
    manager = make_manager()
    manager.upgrade_version_list = ["1.0"]
    monkeypatch.setattr(
        upgrade_config.Readyml,
        "load_yaml",
        lambda _path: (_ for _ in ()).throw(
            ValueError("injected operational failure")
        ),
    )
    install_reporting_probe(
        manager,
        monkeypatch,
        reporter,
        exception_type,
        "配置文件升级失败:",
    )

    assert manager.update_config_file() is False


@pytest.mark.parametrize("indeterminate_path", ["old", "destination"])
def test_ordinary_config_indeterminate_recovery_preserves_all_artifacts(
    configured_tree,
    monkeypatch,
    indeterminate_path,
):
    target = configured_tree.roots["ui_config"] / "indeterminate.bin"
    source = configured_tree.config_dir / "indeterminate.bin"
    target.write_bytes(b"original indeterminate target")
    source.write_bytes(b"replacement indeterminate source")
    manager = make_manager()
    real_rename = os.rename
    real_lexists = manager._snapshot_path_lexists
    install_failed = {"value": False}

    def fail_install(source_path, destination_path):
        if (
            Path(destination_path) == target
            and Path(source_path).name.startswith(
                f".{target.name}.staging-"
            )
        ):
            install_failed["value"] = True
            raise OSError("injected install failure")
        return real_rename(source_path, destination_path)

    def indeterminate_lexists(path):
        if install_failed["value"] and path is not None:
            path = Path(path)
            if (
                indeterminate_path == "old"
                and path.name.startswith(f".{target.name}.old-")
            ):
                return None
            if indeterminate_path == "destination" and path == target:
                return None
        return real_lexists(path)

    monkeypatch.setattr(upgrade_config.os, "rename", fail_install)
    monkeypatch.setattr(
        manager,
        "_snapshot_path_lexists",
        indeterminate_lexists,
    )

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False

    artifacts = {
        path.name: path.read_bytes()
        for path in target.parent.iterdir()
        if path.name.startswith(
            (
                f".{target.name}.old-",
                f".{target.name}.staging-",
            )
        )
    }
    assert not target.exists()
    assert sorted(artifacts.values()) == sorted(
        [
            b"original indeterminate target",
            b"replacement indeterminate source",
        ]
    )


def test_snapshot_path_lexists_none_is_safe():
    assert make_manager()._snapshot_path_lexists(None) is False


@pytest.mark.parametrize("source_kind", ["file", "directory"])
def test_ordinary_config_source_disappearance_during_copy_preserves_target(
    configured_tree,
    monkeypatch,
    source_kind,
):
    entry_name = f"race-{source_kind}"
    config_relative = f"ui/ui_config/{entry_name}"
    target = configured_tree.roots["ui_config"] / entry_name
    source = configured_tree.config_dir / entry_name
    if source_kind == "file":
        target.write_bytes(b"\x00original-file-target\xff")
        source.write_bytes(b"\x00replacement-file-source\xff")
        original_target = target.read_bytes()
    else:
        target.mkdir()
        target.joinpath("original.bin").write_bytes(b"\x00original-directory\xff")
        target.joinpath("retained.txt").write_bytes(b"retained")
        source.mkdir()
        source.joinpath("replacement.bin").write_bytes(b"replacement")
        source.joinpath("new.txt").write_bytes(b"new")
        original_target = snapshot_tree(target)
    original_managed_root = snapshot_tree(
        configured_tree.roots["ui_config"]
    )
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [config_relative],
                    "json_merge": [],
                },
            )
        ],
    )
    real_smart_copy = manager.smart_copy
    race_triggered = {"value": False}

    def remove_source_immediately_before_copy(source_path, destination_path):
        if Path(source_path) == source and not race_triggered["value"]:
            race_triggered["value"] = True
            assert manager.smart_remove(source_path) is True
        return real_smart_copy(source_path, destination_path)

    monkeypatch.setattr(
        manager,
        "smart_copy",
        remove_source_immediately_before_copy,
    )

    assert manager.update_config_file() is False

    assert race_triggered["value"] is True
    if source_kind == "file":
        assert target.is_file()
        assert target.read_bytes() == original_target
    else:
        assert target.is_dir()
        assert snapshot_tree(target) == original_target
    assert snapshot_tree(
        configured_tree.backups["ui_config"]
    ) == original_managed_root
    assert swap_artifacts(target) == []


@pytest.mark.parametrize("source_kind", ["file", "directory"])
def test_ordinary_config_source_swap_during_copy_then_restore_is_rejected(
    configured_tree,
    monkeypatch,
    source_kind,
):
    entry_name = f"content-race-{source_kind}"
    target = configured_tree.roots["ui_config"] / entry_name
    source = configured_tree.config_dir / entry_name
    displaced_source = configured_tree.config_dir / f".{entry_name}.original"
    if source_kind == "file":
        target.write_bytes(b"original target file")
        source.write_bytes(b"validated source file")
        original_target = target.read_bytes()
    else:
        target.mkdir()
        target.joinpath("target.bin").write_bytes(b"original target directory")
        source.mkdir()
        source.joinpath("payload.bin").write_bytes(b"validated directory source")
        original_target = snapshot_tree(target)
    manager = make_manager()
    real_smart_copy = manager.smart_copy
    race_triggered = {"value": False}

    def swap_copy_restore(source_path, staging_path):
        if Path(source_path) != source or race_triggered["value"]:
            return real_smart_copy(source_path, staging_path)
        race_triggered["value"] = True
        os.replace(source, displaced_source)
        if source_kind == "file":
            source.write_bytes(b"unvalidated substituted file")
        else:
            source.mkdir()
            source.joinpath("payload.bin").write_bytes(
                b"unvalidated substituted directory"
            )
        try:
            return real_smart_copy(source_path, staging_path)
        finally:
            assert manager.smart_remove(source_path) is True
            os.replace(displaced_source, source)

    monkeypatch.setattr(manager, "smart_copy", swap_copy_restore)

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False

    assert race_triggered["value"] is True
    if source_kind == "file":
        assert target.read_bytes() == original_target
    else:
        assert snapshot_tree(target) == original_target
    assert swap_artifacts(target) == []


def test_ordinary_config_concurrent_destination_creation_is_preserved(
    configured_tree,
    monkeypatch,
):
    target = configured_tree.roots["ui_config"] / "created-during-copy.bin"
    source = configured_tree.config_dir / "created-during-copy.bin"
    source.write_bytes(b"validated replacement")
    manager = make_manager()
    real_smart_copy = manager.smart_copy
    created_bytes = b"concurrently created destination"

    def create_destination_after_copy(source_path, staging_path):
        result = real_smart_copy(source_path, staging_path)
        target.write_bytes(created_bytes)
        return result

    monkeypatch.setattr(manager, "smart_copy", create_destination_after_copy)

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False
    assert target.read_bytes() == created_bytes
    assert swap_artifacts(target) == []


def test_ordinary_config_concurrent_destination_change_is_preserved(
    configured_tree,
    monkeypatch,
):
    target = configured_tree.roots["ui_config"] / "changed-during-copy.bin"
    source = configured_tree.config_dir / "changed-during-copy.bin"
    target.write_bytes(b"initial destination")
    source.write_bytes(b"validated replacement")
    manager = make_manager()
    real_smart_copy = manager.smart_copy
    changed_bytes = b"concurrently changed destination"

    def change_destination_after_copy(source_path, staging_path):
        result = real_smart_copy(source_path, staging_path)
        target.write_bytes(changed_bytes)
        return result

    monkeypatch.setattr(manager, "smart_copy", change_destination_after_copy)

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False
    assert target.read_bytes() == changed_bytes
    assert swap_artifacts(target) == []


def test_ordinary_config_absent_destination_install_is_atomic_no_clobber(
    configured_tree,
    monkeypatch,
):
    target = configured_tree.roots["ui_config"] / "absent-install-race.bin"
    source = configured_tree.config_dir / target.name
    source.write_bytes(b"validated replacement")
    manager = make_manager()
    real_rename = os.rename
    concurrent_bytes = b"concurrent destination created before install"
    race_triggered = {"value": False}

    def create_destination_immediately_before_install(
        source_path,
        destination_path,
    ):
        if (
            Path(destination_path) == target
            and Path(source_path).name.startswith(
                f".{target.name}.staging-"
            )
            and not race_triggered["value"]
        ):
            target.write_bytes(concurrent_bytes)
            race_triggered["value"] = True
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        create_destination_immediately_before_install,
    )

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False
    assert race_triggered["value"] is True
    assert target.read_bytes() == concurrent_bytes
    assert not any(
        path.name.startswith(f".{target.name}.old-")
        for path in target.parent.iterdir()
    )


def test_ordinary_config_destination_move_verifies_exact_moved_identity(
    configured_tree,
    monkeypatch,
):
    target = configured_tree.roots["ui_config"] / "move-identity-race.bin"
    source = configured_tree.config_dir / target.name
    target.write_bytes(b"original recovery data")
    source.write_bytes(b"validated replacement")
    displaced_original = target.with_name(f".{target.name}.concurrent-original")
    manager = make_manager()
    real_rename = os.rename
    concurrent_bytes = b"concurrent replacement moved to recovery slot"
    race_triggered = {"value": False}

    def replace_destination_immediately_before_move(
        source_path,
        destination_path,
    ):
        if (
            Path(source_path) == target
            and Path(destination_path).name.startswith(
                f".{target.name}.old-"
            )
            and not race_triggered["value"]
        ):
            real_rename(target, displaced_original)
            target.write_bytes(concurrent_bytes)
            race_triggered["value"] = True
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        replace_destination_immediately_before_move,
    )

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False
    assert race_triggered["value"] is True
    assert target.read_bytes() == concurrent_bytes
    assert displaced_original.read_bytes() == b"original recovery data"
    assert not any(
        path.name.startswith(f".{target.name}.old-")
        for path in target.parent.iterdir()
    )


def test_ordinary_config_existing_destination_install_is_atomic_no_clobber(
    configured_tree,
    monkeypatch,
):
    target = configured_tree.roots["ui_config"] / "existing-install-race.bin"
    source = configured_tree.config_dir / target.name
    original_bytes = b"original recovery data"
    concurrent_bytes = b"concurrent destination before staging install"
    target.write_bytes(original_bytes)
    source.write_bytes(b"validated replacement")
    manager = make_manager()
    real_rename = os.rename
    race_triggered = {"value": False}

    def create_destination_immediately_before_install(
        source_path,
        destination_path,
    ):
        if (
            Path(destination_path) == target
            and Path(source_path).name.startswith(
                f".{target.name}.staging-"
            )
            and not race_triggered["value"]
        ):
            target.write_bytes(concurrent_bytes)
            race_triggered["value"] = True
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        create_destination_immediately_before_install,
    )

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False
    assert race_triggered["value"] is True
    assert target.read_bytes() == concurrent_bytes
    recovery_artifacts = [
        path
        for path in target.parent.iterdir()
        if path.name.startswith(f".{target.name}.old-")
    ]
    assert len(recovery_artifacts) == 1
    assert recovery_artifacts[0].read_bytes() == original_bytes


def test_ordinary_config_recovery_is_atomic_no_clobber(
    configured_tree,
    monkeypatch,
):
    target = configured_tree.roots["ui_config"] / "recovery-race.bin"
    source = configured_tree.config_dir / target.name
    original_bytes = b"original recovery data"
    concurrent_bytes = b"concurrent destination before recovery"
    target.write_bytes(original_bytes)
    source.write_bytes(b"validated replacement")
    manager = make_manager()
    real_rename = os.rename
    race_triggered = {"value": False}

    def fail_install_then_create_destination_before_recovery(
        source_path,
        destination_path,
    ):
        source_path = Path(source_path)
        destination_path = Path(destination_path)
        if (
            destination_path == target
            and source_path.name.startswith(f".{target.name}.staging-")
        ):
            raise OSError("injected staging install failure")
        if (
            destination_path == target
            and source_path.name.startswith(f".{target.name}.old-")
            and not race_triggered["value"]
        ):
            target.write_bytes(concurrent_bytes)
            race_triggered["value"] = True
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        fail_install_then_create_destination_before_recovery,
    )

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False
    assert race_triggered["value"] is True
    assert target.read_bytes() == concurrent_bytes
    recovery_artifacts = [
        path
        for path in target.parent.iterdir()
        if path.name.startswith(f".{target.name}.old-")
    ]
    assert len(recovery_artifacts) == 1
    assert recovery_artifacts[0].read_bytes() == original_bytes


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_ordinary_config_nested_parent_junction_substitution_cannot_mutate_external(
    configured_tree,
    monkeypatch,
):
    parent = configured_tree.roots["ui_config"] / "nested"
    parent.mkdir()
    target = parent / "parent-race.bin"
    target.write_bytes(b"original managed target")
    source = configured_tree.config_dir / target.name
    source.write_bytes(b"validated replacement")
    external = configured_tree.target_dir.parent / "external-parent"
    external.mkdir()
    external_target = external / target.name
    external_target.write_bytes(b"external target must survive")
    external_witness = external / "witness.bin"
    external_witness.write_bytes(b"external witness must survive")
    displaced_parent = parent.with_name("nested-displaced")
    manager = make_manager()
    real_rename = os.rename
    substitution_attempted = {"value": False}

    def substitute_parent_at_destination_transition(
        source_path,
        destination_path,
    ):
        if (
            Path(source_path) == target
            and Path(destination_path).name.startswith(
                f".{target.name}.old-"
            )
            and not substitution_attempted["value"]
        ):
            substitution_attempted["value"] = True
            real_rename(parent, displaced_parent)
            create_windows_junction(external, parent)
        return real_rename(source_path, destination_path)

    monkeypatch.setattr(
        upgrade_config.os,
        "rename",
        substitute_parent_at_destination_transition,
    )

    assert manager._replace_with_staged_ordinary_config(
        str(source),
        str(target),
    ) is False

    assert substitution_attempted["value"] is True
    assert not os.path.isjunction(parent)
    assert target.read_bytes() == b"original managed target"
    assert external_target.read_bytes() == b"external target must survive"
    assert external_witness.read_bytes() == b"external witness must survive"
    assert swap_artifacts(target) == []


@pytest.mark.parametrize("target_is_directory", [False, True])
def test_cleanup_snapshot_artifact_removes_dangling_links_without_following(
    tmp_path,
    target_is_directory,
):
    manager = make_manager()
    missing_target = tmp_path / (
        "missing-directory" if target_is_directory else "missing-file"
    )
    artifact = tmp_path / (
        "dangling-directory-link"
        if target_is_directory
        else "dangling-file-link"
    )
    create_symlink_or_skip(
        missing_target,
        artifact,
        target_is_directory=target_is_directory,
    )

    assert os.path.lexists(artifact)
    assert manager._cleanup_snapshot_artifact(str(artifact)) is True
    assert not os.path.lexists(artifact)
    assert not missing_target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_cleanup_snapshot_artifact_removes_junction_without_following(
    tmp_path,
):
    manager = make_manager()
    external = tmp_path / "external"
    external.mkdir()
    external.joinpath("must-survive.bin").write_bytes(b"external data")
    artifact = tmp_path / "artifact-junction"
    create_windows_junction(external, artifact)

    assert manager._cleanup_snapshot_artifact(str(artifact)) is True
    assert not os.path.lexists(artifact)
    assert external.joinpath("must-survive.bin").read_bytes() == b"external data"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
@pytest.mark.parametrize("source_kind", ["file", "directory"])
def test_ordinary_config_external_junction_substitution_during_copy_is_rejected(
    configured_tree,
    monkeypatch,
    source_kind,
):
    entry_name = f"junction-race-{source_kind}"
    config_relative = f"ui/ui_config/{entry_name}"
    target = configured_tree.roots["ui_config"] / entry_name
    source = configured_tree.config_dir / entry_name
    if source_kind == "file":
        target.write_bytes(b"\x00original-file-target\xff")
        source.write_bytes(b"\x00replacement-file-source\xff")
        original_target = target.read_bytes()
    else:
        target.mkdir()
        target.joinpath("original.bin").write_bytes(b"\x00original-directory\xff")
        target.joinpath("retained.txt").write_bytes(b"retained")
        source.mkdir()
        source.joinpath("replacement.bin").write_bytes(b"replacement")
        source.joinpath("new.txt").write_bytes(b"new")
        original_target = snapshot_tree(target)
    original_managed_root = snapshot_tree(
        configured_tree.roots["ui_config"]
    )
    external_source = (
        configured_tree.package_dir / f"external-{source_kind}-source"
    )
    external_source.mkdir()
    external_source.joinpath("external.bin").write_bytes(
        b"\x00external-content-must-not-be-installed\xff"
    )
    expected_external_tree = snapshot_tree(external_source)
    displaced_source = (
        configured_tree.config_dir / f".{entry_name}.displaced"
    )
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [config_relative],
                    "json_merge": [],
                },
            )
        ],
    )
    real_smart_copy = manager.smart_copy
    race_triggered = {"value": False}

    def substitute_junction_immediately_before_copy(
        source_path,
        destination_path,
    ):
        if Path(source_path) == source and not race_triggered["value"]:
            os.replace(source, displaced_source)
            create_windows_junction(external_source, source)
            race_triggered["value"] = True
        return real_smart_copy(source_path, destination_path)

    monkeypatch.setattr(
        manager,
        "smart_copy",
        substitute_junction_immediately_before_copy,
    )

    assert manager.update_config_file() is False

    assert race_triggered["value"] is True
    if source_kind == "file":
        assert target.is_file()
        assert target.read_bytes() == original_target
    else:
        assert target.is_dir()
        assert snapshot_tree(target) == original_target
    assert snapshot_tree(external_source) == expected_external_tree
    assert snapshot_tree(
        configured_tree.backups["ui_config"]
    ) == original_managed_root
    assert swap_artifacts(target) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_directory_manifest_binds_lstat_entry_until_enumeration_finishes(
    configured_tree,
    monkeypatch,
):
    source = configured_tree.config_dir / "manifest-directory-race"
    source.mkdir()
    source.joinpath("validated.bin").write_bytes(b"validated source")
    displaced_source = source.with_name(f".{source.name}.displaced")
    external = configured_tree.package_dir / "external-manifest-data"
    external.mkdir()
    external_secret = external / "secret.bin"
    external_secret.write_bytes(b"external data must never be traversed")
    target = (
        configured_tree.roots["ui_config"]
        / "manifest-directory-race"
    )
    target.mkdir()
    target.joinpath("original.bin").write_bytes(b"original target")
    original_target = snapshot_tree(target)
    manager = make_manager()
    real_scandir = os.scandir
    real_file_digest = manager._ordinary_config_file_digest
    substitution_attempted = {"value": False}
    substitution_blocked = {"value": False}

    def substitute_junction_between_lstat_and_enumeration(path):
        if (
            Path(path) == source
            and not substitution_attempted["value"]
        ):
            substitution_attempted["value"] = True
            try:
                os.rename(source, displaced_source)
                create_windows_junction(external, source)
            except OSError:
                substitution_blocked["value"] = True
                raise OSError("injected manifest enumeration failure")
        return real_scandir(path)

    def reject_external_hash(path, expected_stat):
        if os.path.samefile(os.path.dirname(path), external):
            pytest.fail("manifest traversed external junction data")
        return real_file_digest(path, expected_stat)

    monkeypatch.setattr(
        upgrade_config.os,
        "scandir",
        substitute_junction_between_lstat_and_enumeration,
    )
    monkeypatch.setattr(
        manager,
        "_ordinary_config_file_digest",
        reject_external_hash,
    )

    try:
        assert manager._replace_with_staged_ordinary_config(
            str(source),
            str(target),
        ) is False
        assert substitution_attempted["value"] is True
        assert substitution_blocked["value"] is True
        assert snapshot_tree(target) == original_target
        assert external_secret.read_bytes() == (
            b"external data must never be traversed"
        )
        assert swap_artifacts(target) == []
    finally:
        if os.path.isjunction(source):
            os.rmdir(source)
        if displaced_source.exists() and not source.exists():
            os.rename(displaced_source, source)


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
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "state": "pre-upgrade"
    }
    assert any(
        "target_conflicts_with_config" in message
        for message in json_merge_messages(manager, "error")
    )


@pytest.mark.parametrize(
    "config_target",
    [
        pytest.param("ui/ui_config/./same.json", id="current-directory"),
        pytest.param(
            "ui/ui_config/sub/../same.json",
            id="parent-directory",
        ),
        pytest.param(
            r"ui\ui_config\same.json",
            id="windows-versus-forward-separators",
        ),
    ],
)
def test_config_path_alias_conflicts_with_json_merge_target_before_merge_write(
    configured_tree,
    monkeypatch,
    config_target,
):
    merge_target = "ui/ui_config/same.json"
    target = write_json(
        configured_tree.roots["ui_config"] / "same.json",
        {"state": "pre-upgrade"},
    )
    (target.parent / "sub").mkdir()
    replacement_name = config_target.split("/")[-1]
    write_json(
        configured_tree.config_dir / replacement_name,
        {"state": "replacement"},
    )
    write_json(configured_tree.config_dir / "patch.json", {"added": True})
    helper_calls = []
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [config_target],
                    "json_merge": [
                        {
                            "target": merge_target,
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
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "state": "pre-upgrade"
    }
    assert any(
        "target_conflicts_with_config" in message
        for message in json_merge_messages(manager, "error")
    )


@pytest.mark.parametrize(
    ("case_name", "expected_category"),
    [
        ("non_string", "non_string_path"),
        ("empty", "empty_path"),
        ("whitespace", "empty_path"),
        (
            "absolute",
            "drive_qualified_path" if os.name == "nt" else "rooted_path",
        ),
        ("leading_forward_separator", "rooted_path"),
        ("leading_back_separator", "rooted_path"),
        ("drive_relative", "drive_qualified_path"),
        ("drive_rooted", "drive_qualified_path"),
        ("nul", "nul_path"),
    ],
)
def test_invalid_ordinary_config_destinations_fail_before_backup_or_writes(
    configured_tree,
    monkeypatch,
    case_name,
    expected_category,
):
    merge_target = write_json(
        configured_tree.roots["ui_config"] / "same.json",
        {"state": "pre-upgrade"},
    )
    write_json(configured_tree.config_dir / "patch.json", {"added": True})

    invalid_config_entry = {
        "non_string": 42,
        "empty": "",
        "whitespace": "   ",
        "absolute": str(configured_tree.target_dir / "outside.json"),
        "leading_forward_separator": "/ui/ui_config/same.json",
        "leading_back_separator": r"\ui\ui_config\same.json",
        "drive_relative": r"C:ui\ui_config\same.json",
        "drive_rooted": r"C:\ui\ui_config\same.json",
        "nul": "ui/ui_config/\x00same.json",
    }[case_name]
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": [invalid_config_entry],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/same.json",
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            )
        ],
    )
    original_target_bytes = merge_target.read_bytes()
    mutation_calls = track_config_update_mutations(manager, monkeypatch)

    assert manager.update_config_file() is False

    assert mutation_calls == []
    assert merge_target.read_bytes() == original_target_bytes
    errors = "\n".join(logged_messages(manager, "error"))
    assert f"ordinary_config error: destination_path:{expected_category}" in errors


def test_backup_rejects_invalid_ordinary_destination_before_snapshot(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    snapshot_calls = []
    monkeypatch.setattr(
        manager,
        "_replace_with_staged_snapshot",
        lambda source, destination: (
            snapshot_calls.append((source, destination)) or True
        ),
    )

    assert manager.backup_config_file(["/ui/ui_config/same.json"]) is False

    assert snapshot_calls == []
    assert any(
        "ordinary_config error: destination_path:rooted_path" in message
        for message in logged_messages(manager, "error")
    )


def test_dangling_ordinary_config_destination_fails_before_backup_or_writes(
    configured_tree,
    monkeypatch,
):
    dangling_target = configured_tree.roots["ui_config"] / "dangling.json"
    create_symlink_or_skip(
        configured_tree.roots["ui_config"] / "missing.json",
        dangling_target,
    )
    merge_target = write_json(
        configured_tree.roots["ui_config"] / "merged.json",
        {"state": "pre-upgrade"},
    )
    write_json(configured_tree.config_dir / "dangling.json", {"replacement": True})
    write_json(configured_tree.config_dir / "patch.json", {"added": True})
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_config/dangling.json"],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/merged.json",
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            )
        ],
    )
    original_merge_target_bytes = merge_target.read_bytes()
    mutation_calls = track_config_update_mutations(manager, monkeypatch)

    assert manager.update_config_file() is False

    assert mutation_calls == []
    assert os.path.lexists(dangling_target)
    assert not dangling_target.exists()
    assert merge_target.read_bytes() == original_merge_target_bytes
    assert any(
        "ordinary_config error: destination_path:dangling_link" in message
        for message in logged_messages(manager, "error")
    )


def test_samefile_error_fails_conflict_preflight_before_backup_or_writes(
    configured_tree,
    monkeypatch,
):
    ordinary_target = write_json(
        configured_tree.roots["ui_config"] / "ordinary.json",
        {"state": "pre-upgrade"},
    )
    merge_target = write_json(
        configured_tree.roots["ui_config"] / "merged.json",
        {"state": "pre-upgrade"},
    )
    write_json(configured_tree.config_dir / "ordinary.json", {"replacement": True})
    write_json(configured_tree.config_dir / "patch.json", {"added": True})
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_config/ordinary.json"],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/merged.json",
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            )
        ],
    )
    original_ordinary_bytes = ordinary_target.read_bytes()
    original_merge_bytes = merge_target.read_bytes()
    mutation_calls = track_config_update_mutations(manager, monkeypatch)

    monkeypatch.setattr(
        upgrade_config.os.path,
        "samefile",
        lambda *_paths: (_ for _ in ()).throw(OSError("injected samefile error")),
    )

    assert manager.update_config_file() is False

    assert mutation_calls == []
    assert ordinary_target.read_bytes() == original_ordinary_bytes
    assert merge_target.read_bytes() == original_merge_bytes
    assert any(
        "json_merge error: destination_identity_check_failed" in message
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


def create_junction_or_skip(source, link):
    if os.name != "nt":
        pytest.skip("directory junctions are only available on Windows")

    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"directory junctions are unavailable: {result.stderr}")


def create_windows_junction(source, link):
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(source)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def assert_ordinary_source_escape_is_rejected_before_target_mutation(
    configured_tree,
    create_source_alias,
):
    target_root = configured_tree.roots["ui_pic"]
    original_target_tree = snapshot_tree(target_root)
    external_source = configured_tree.package_dir / "external-ui-pic"
    external_source.mkdir()
    (external_source / "marker.txt").write_text(
        "external replacement",
        encoding="utf-8",
    )
    source_alias = configured_tree.config_dir / "ui_pic"
    create_source_alias(external_source, source_alias)
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_pic"],
                    "json_merge": [],
                },
            )
        ],
    )

    assert manager.update_config_file() is False

    assert snapshot_tree(target_root) == original_target_tree
    assert os.path.samefile(source_alias, external_source)
    assert manager.ui_pic_flag is True
    assert not configured_tree.backups["ui_pic"].exists()
    assert any(
        "ordinary_config error: source_path:symlink_escape" in message
        for message in logged_messages(manager, "error")
    )


def test_ordinary_source_rejects_symlink_escape_before_target_mutation(
    configured_tree,
):
    assert_ordinary_source_escape_is_rejected_before_target_mutation(
        configured_tree,
        lambda source, link: create_symlink_or_skip(
            source,
            link,
            target_is_directory=True,
        ),
    )


def test_ordinary_source_rejects_junction_escape_before_target_mutation(
    configured_tree,
):
    assert_ordinary_source_escape_is_rejected_before_target_mutation(
        configured_tree,
        create_junction_or_skip,
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_backup_rejects_external_junction_before_snapshot_writes(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    managed_root = configured_tree.roots["ui_config"]
    external_root = configured_tree.target_dir.parent / "external-data"
    external_root.mkdir()
    external_file = external_root / "secret.bin"
    external_file.write_bytes(b"\x00external-secret\xff")
    junction = managed_root / "external-junction"
    create_windows_junction(external_root, junction)
    copy_calls = []
    monkeypatch.setattr(
        manager,
        "smart_copy",
        lambda source, destination: (
            copy_calls.append((source, destination)) or True
        ),
    )

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is False

    assert manager.ui_config_flag is True
    assert copy_calls == []
    assert not configured_tree.backup_dir.exists()
    assert not configured_tree.backups["ui_config"].exists()
    assert os.path.samefile(junction, external_root)
    assert external_file.read_bytes() == b"\x00external-secret\xff"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
@pytest.mark.parametrize(
    "escape_location",
    ["backup_dir", "backup_parent", "managed_root_ancestor"],
)
def test_backup_rejects_external_ancestor_junction_without_victim_mutation(
    configured_tree,
    monkeypatch,
    escape_location,
):
    manager = make_manager()
    external_root = (
        configured_tree.target_dir.parent
        / f"external-ancestor-{escape_location}"
    )
    external_root.mkdir()
    external_root.joinpath("victim.bin").write_bytes(
        b"\x00external-ancestor-victim\xff"
    )

    if escape_location == "backup_dir":
        create_windows_junction(
            external_root,
            configured_tree.backup_dir,
        )
    elif escape_location == "backup_parent":
        backup_parent = configured_tree.package_dir / "backup-parent"
        backup_dir = backup_parent / "backup"
        backup_path = backup_dir / "ui_config"
        monkeypatch.setattr(
            upgrade_config,
            "BACKUP_DIR",
            directory_path(backup_dir),
        )
        monkeypatch.setattr(
            upgrade_config,
            "BACKUP_UI_CONFIG_PATH",
            directory_path(backup_path),
        )
        create_windows_junction(external_root, backup_parent)
    else:
        managed_parent = configured_tree.roots["ui_config"].parent
        external_managed_parent = external_root / "ui"
        os.replace(managed_parent, external_managed_parent)
        create_windows_junction(
            external_managed_parent,
            managed_parent,
        )

    expected_external_tree = snapshot_tree(external_root)

    backup_result = manager.backup_config_file(
        ["ui/ui_config/settings.json"]
    )

    assert snapshot_tree(external_root) == expected_external_tree
    assert backup_result is False
    assert manager.ui_config_flag is True


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
@pytest.mark.parametrize(
    "escape_moment",
    ["immediately_before_staging", "immediately_before_replacement"],
)
def test_backup_revalidates_destination_ancestors_at_swap_boundaries(
    configured_tree,
    monkeypatch,
    escape_moment,
):
    manager = make_manager()
    backup_dir = configured_tree.backup_dir
    displaced_backup_dir = (
        configured_tree.package_dir
        / f"displaced-backup-{escape_moment}"
    )
    external_root = (
        configured_tree.target_dir.parent
        / f"external-race-{escape_moment}"
    )
    external_root.mkdir()
    external_root.joinpath("victim.bin").write_bytes(
        b"\x00external-race-victim\xff"
    )
    expected_external_tree = snapshot_tree(external_root)
    race_triggered = {"value": False}

    def install_external_backup_junction():
        os.replace(backup_dir, displaced_backup_dir)
        create_windows_junction(external_root, backup_dir)
        race_triggered["value"] = True

    if escape_moment == "immediately_before_staging":
        real_makedirs = os.makedirs
        backup_makedirs_calls = {"count": 0}

        def race_after_second_backup_makedirs(path, *args, **kwargs):
            result = real_makedirs(path, *args, **kwargs)
            if Path(path) == backup_dir:
                backup_makedirs_calls["count"] += 1
                if backup_makedirs_calls["count"] == 2:
                    install_external_backup_junction()
            return result

        monkeypatch.setattr(
            upgrade_config.os,
            "makedirs",
            race_after_second_backup_makedirs,
        )
    else:
        real_smart_copy = manager.smart_copy

        def race_after_staging_copy(source, destination):
            result = real_smart_copy(source, destination)
            install_external_backup_junction()
            return result

        monkeypatch.setattr(
            manager,
            "smart_copy",
            race_after_staging_copy,
        )

    backup_result = manager.backup_config_file(
        ["ui/ui_config/settings.json"]
    )

    assert race_triggered["value"] is True
    assert snapshot_tree(external_root) == expected_external_tree
    assert backup_result is False
    assert manager.ui_config_flag is True


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
@pytest.mark.parametrize("upgrade_uses_alias", [True, False])
def test_contained_junction_backup_and_restore_preserve_snapshot_and_alias(
    configured_tree,
    upgrade_uses_alias,
):
    manager = make_manager()
    managed_root = configured_tree.roots["ui_config"]
    actual_directory = managed_root / "actual"
    actual_directory.mkdir()
    actual_marker = actual_directory / "alias-marker.bin"
    actual_marker.write_bytes(b"\x00original-alias-data\xff")
    alias_directory = managed_root / "alias"
    create_windows_junction(actual_directory, alias_directory)

    if upgrade_uses_alias:
        target = actual_directory / "settings.json"
        target_relative = "ui/ui_config/alias/settings.json"
    else:
        target = managed_root / "settings.json"
        target_relative = "ui/ui_config/settings.json"
    target.write_bytes(b'{"state":"original"}')

    assert manager.backup_config_file([target_relative]) is True
    assert os.path.isjunction(alias_directory)
    assert os.path.samefile(alias_directory, actual_directory)

    if upgrade_uses_alias:
        alias_directory.joinpath("settings.json").write_bytes(
            b'{"state":"changed-through-alias"}'
        )
    else:
        target.write_bytes(b'{"state":"changed-directly"}')
        alias_directory.joinpath("alias-marker.bin").write_bytes(
            b"changed unrelated alias data"
        )

    assert manager.restore_config() is True

    assert target.read_bytes() == b'{"state":"original"}'
    assert actual_marker.read_bytes() == b"\x00original-alias-data\xff"
    assert os.path.isjunction(alias_directory)
    assert os.path.samefile(alias_directory, actual_directory)
    assert not configured_tree.backups["ui_config"].exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_backup_rejects_junction_cycle_without_traversing_it(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    managed_root = configured_tree.roots["ui_config"]
    original_marker = managed_root.joinpath("marker.txt").read_bytes()
    cycle = managed_root / "cycle"
    create_windows_junction(managed_root, cycle)
    copy_calls = []
    monkeypatch.setattr(
        manager,
        "smart_copy",
        lambda source, destination: (
            copy_calls.append((source, destination)) or True
        ),
    )

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is False

    assert manager.ui_config_flag is True
    assert copy_calls == []
    assert not configured_tree.backup_dir.exists()
    assert not configured_tree.backups["ui_config"].exists()
    assert os.path.samefile(cycle, managed_root)
    assert managed_root.joinpath("marker.txt").read_bytes() == original_marker


def test_backup_rejects_external_directory_symlink_when_available(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    managed_root = configured_tree.roots["ui_config"]
    external_root = configured_tree.target_dir.parent / "external-symlink-data"
    external_root.mkdir()
    external_file = external_root / "secret.bin"
    external_file.write_bytes(b"external symlink bytes")
    link = managed_root / "external-symlink"
    create_symlink_or_skip(
        external_root,
        link,
        target_is_directory=True,
    )
    copy_calls = []
    monkeypatch.setattr(
        manager,
        "smart_copy",
        lambda source, destination: (
            copy_calls.append((source, destination)) or True
        ),
    )

    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is False

    assert manager.ui_config_flag is True
    assert copy_calls == []
    assert not configured_tree.backup_dir.exists()
    assert os.path.samefile(link, external_root)
    assert external_file.read_bytes() == b"external symlink bytes"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_restore_preserves_external_junction_instead_of_replacing_it(
    configured_tree,
    monkeypatch,
):
    manager = make_manager()
    managed_root = configured_tree.roots["ui_config"]
    linked_path = managed_root / "linked"
    linked_path.mkdir()
    linked_path.joinpath("local.txt").write_bytes(b"snapshot-local-data")
    assert manager.backup_config_file(["ui/ui_config/settings.json"]) is True

    os.replace(linked_path, managed_root / "original-linked-directory")
    external_root = configured_tree.target_dir.parent / "restore-external"
    external_root.mkdir()
    external_file = external_root / "external.txt"
    external_file.write_bytes(b"external restore data")
    create_windows_junction(external_root, linked_path)
    copy_calls = []
    monkeypatch.setattr(
        manager,
        "smart_copy",
        lambda source, destination: (
            copy_calls.append((source, destination)) or True
        ),
    )

    assert manager.restore_config() is False

    assert copy_calls == []
    assert configured_tree.backups["ui_config"].exists()
    assert os.path.samefile(linked_path, external_root)
    assert external_file.read_bytes() == b"external restore data"


def assert_config_parent_alias_conflict_is_preflighted(
    configured_tree,
    monkeypatch,
    create_parent_alias,
    target_exists=True,
):
    actual_parent = configured_tree.roots["ui_config"] / "actual"
    actual_parent.mkdir()
    actual_target = actual_parent / "same.json"
    if target_exists:
        write_json(actual_target, {"state": "pre-upgrade"})
    alias_parent = configured_tree.roots["ui_config"] / "link"
    create_parent_alias(actual_parent, alias_parent)
    write_json(
        configured_tree.config_dir / "same.json",
        {"state": "replacement"},
    )
    write_json(configured_tree.config_dir / "patch.json", {"added": True})
    original_target_bytes = (
        actual_target.read_bytes() if target_exists else None
    )
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_config/link/same.json"],
                    "json_merge": [
                        {
                            "target": "ui/ui_config/actual/same.json",
                            "source": "resources/config/patch.json",
                        }
                    ],
                },
            )
        ],
    )
    ordinary_write_calls = []
    helper_calls = []

    monkeypatch.setattr(manager, "backup_config_file", lambda _paths: True)
    monkeypatch.setattr(
        manager,
        "smart_remove",
        lambda path: ordinary_write_calls.append(("remove", path)) or True,
    )
    monkeypatch.setattr(
        manager,
        "smart_copy",
        lambda source, target: (
            ordinary_write_calls.append(("copy", source, target)) or True
        ),
    )
    monkeypatch.setattr(
        upgrade_config,
        "merge_json_files_with_reason",
        lambda *args: helper_calls.append(args) or (True, "ok"),
        raising=False,
    )

    assert manager.update_config_file() is False

    assert ordinary_write_calls == []
    assert helper_calls == []
    if target_exists:
        assert actual_target.read_bytes() == original_target_bytes
    else:
        assert not actual_target.exists()
    assert any(
        "target_conflicts_with_config" in message
        for message in json_merge_messages(manager, "error")
    )


def test_config_symlinked_parent_alias_conflicts_before_ordinary_replacement(
    configured_tree,
    monkeypatch,
):
    assert_config_parent_alias_conflict_is_preflighted(
        configured_tree,
        monkeypatch,
        lambda source, link: create_symlink_or_skip(
            source,
            link,
            target_is_directory=True,
        ),
    )


def test_config_junction_parent_alias_conflicts_before_ordinary_replacement(
    configured_tree,
    monkeypatch,
):
    assert_config_parent_alias_conflict_is_preflighted(
        configured_tree,
        monkeypatch,
        create_junction_or_skip,
    )


def test_config_symlinked_parent_alias_conflicts_for_nonexistent_leaf(
    configured_tree,
    monkeypatch,
):
    assert_config_parent_alias_conflict_is_preflighted(
        configured_tree,
        monkeypatch,
        lambda source, link: create_symlink_or_skip(
            source,
            link,
            target_is_directory=True,
        ),
        target_exists=False,
    )


def test_config_junction_parent_alias_conflicts_for_nonexistent_leaf(
    configured_tree,
    monkeypatch,
):
    assert_config_parent_alias_conflict_is_preflighted(
        configured_tree,
        monkeypatch,
        create_junction_or_skip,
        target_exists=False,
    )


def assert_ordinary_config_parent_escape_is_rejected(
    configured_tree,
    create_parent_alias,
):
    external_root = configured_tree.target_dir.parent / "external"
    external_root.mkdir()
    external_target = external_root / "ordinary.json"
    external_target.write_bytes(b"external original bytes")
    unrelated_external_file = external_root / "unrelated.bin"
    unrelated_external_file.write_bytes(b"\x00unrelated external data\xff")
    escaped_parent = configured_tree.roots["ui_config"] / "escape"
    create_parent_alias(external_root, escaped_parent)
    write_json(
        configured_tree.config_dir / "ordinary.json",
        {"state": "replacement"},
    )
    manager = configure_manifest_manager(
        configured_tree,
        [
            (
                "1.0",
                {
                    "config": ["ui/ui_config/escape/ordinary.json"],
                    "json_merge": [],
                },
            )
        ],
    )
    original_external_bytes = external_target.read_bytes()
    original_unrelated_bytes = unrelated_external_file.read_bytes()

    update_result = manager.update_config_file()
    flags_after_update = (
        manager.ui_pic_flag,
        manager.ui_config_flag,
        manager.ai_model_config_flag,
        manager.scannre_barcode_config_flag,
    )
    backup_was_written = configured_tree.backups["ui_config"].exists()
    external_bytes_after_update = external_target.read_bytes()
    restore_result = manager.restore_config()

    assert update_result is False
    assert flags_after_update == (True, True, True, True)
    assert backup_was_written is False
    assert external_bytes_after_update == original_external_bytes
    assert restore_result is True
    assert external_target.read_bytes() == original_external_bytes
    assert unrelated_external_file.read_bytes() == original_unrelated_bytes
    assert os.path.samefile(escaped_parent, external_root)
    assert any(
        "ordinary_config error: destination_path:symlink_escape" in message
        for message in logged_messages(manager, "error")
    )


def test_ordinary_config_rejects_symlinked_parent_escape_before_backup_or_write(
    configured_tree,
):
    assert_ordinary_config_parent_escape_is_rejected(
        configured_tree,
        lambda source, link: create_symlink_or_skip(
            source,
            link,
            target_is_directory=True,
        ),
    )


def test_ordinary_config_rejects_junction_parent_escape_before_backup_or_write(
    configured_tree,
):
    assert_ordinary_config_parent_escape_is_rejected(
        configured_tree,
        create_junction_or_skip,
    )


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
