import os
import sys
import shutil

from control.upgrade_db import DataBaseManager, get_version_database_path, read_current_version_from_db
from control.upgrade_config import ConfigManager
from control.upgrade_exe import ExeUpgradeManager
from log_model.log_manager import LogManager
from consts.program_config import DEFAULT_DIR, DEFAULT_TARGET_DIR, DATABASE_PATH, BACKUP_DIR


def update_version_program(
    logger,
):  # local_program_path: 异音软件可执行程序位置     targer_program_path：目标版本可执行程序位置
    _print_safely("正在更新主程序...")
    _log_safely(logger, "info", "正在更新主程序...")
    try:
        targer_program_path = DEFAULT_DIR + "resources/DiTing.exe"
        local_program_path = DEFAULT_TARGET_DIR + "DiTing.exe"
        shutil.copy2(targer_program_path, local_program_path)
    except Exception as e:
        failure_message = f"更新主程序失败: {e}"
        _log_safely(logger, "error", failure_message)
        return False, failure_message

    _print_safely("更新主程序成功")
    _log_safely(logger, "info", "更新主程序成功")
    return True, "更新主程序成功"


def get_current_version():
    try:
        return read_current_version_from_db(get_version_database_path())
    except Exception as e:
        print(f"获取当前版本失败: {e}")


class _UpgradeStageFailed(Exception):
    pass


def _log_safely(logger, level, message):
    try:
        getattr(logger, level)(message)
    except Exception:
        pass


def _print_safely(message):
    try:
        print(message)
    except Exception:
        pass


def _require_success(result, stage_name):
    if not result:
        raise _UpgradeStageFailed(f"{stage_name}失败")


def _restore_after_failure(logger, config_manager):
    _print_safely("更新失败，用备份的数据替换")
    _log_safely(logger, "error", "更新失败，用备份的数据替换")

    try:
        _print_safely("正在恢复数据库...")
        database_restored = DataBaseManager.restore_db()
        if database_restored is False:
            _log_safely(logger, "error", "数据库恢复失败，保留数据库备份")
    except Exception as e:
        _log_safely(logger, "error", f"数据库恢复异常，保留数据库备份: {e}")

    if config_manager is None:
        _log_safely(logger, "error", "配置管理器未初始化，无法恢复配置文件")
        return

    try:
        _print_safely("正在恢复配置文件...")
        config_restored = config_manager.restore_config()
        if config_restored is False:
            _log_safely(logger, "error", "配置文件恢复失败，保留配置备份")
    except Exception as e:
        _log_safely(logger, "error", f"配置文件恢复异常，保留配置备份: {e}")


def _cleanup_database_backup(logger):
    backup_path = os.path.join(BACKUP_DIR, "audio_data_backup.db")
    try:
        if not os.path.exists(backup_path):
            return True
        removed = os.remove(backup_path)
        if removed is False:
            _log_safely(
                logger,
                "warning",
                f"数据库备份删除失败，保留备份: {backup_path}",
            )
            return False
        return True
    except Exception as e:
        _log_safely(
            logger,
            "warning",
            f"数据库备份删除异常，保留备份: {backup_path}, 错误: {e}",
        )
        return False


def _cleanup_config_backups(logger, config_manager):
    try:
        deleted = config_manager.delete_backup_file()
        if deleted is False:
            _log_safely(logger, "warning", "配置备份删除失败，保留未删除的备份")
            return False
        return True
    except Exception as e:
        _log_safely(
            logger,
            "warning",
            f"配置备份删除异常，保留未删除的备份: {e}",
        )
        return False


def run_upgrade(logger=None):
    if logger is None:
        try:
            logger = LogManager.set_log_handler("core")
        except Exception as e:
            _print_safely(f"日志初始化失败: {e}")
            return False

    try:
        result_backup = DataBaseManager.backup_db()
    except Exception as e:
        _log_safely(logger, "error", f"数据库备份异常: {e}")
        return False
    if not result_backup:
        _log_safely(logger, "error", "数据库备份失败")
        return False

    config_manager = None
    try:
        current_version = get_current_version()
        config_manager = ConfigManager(logger, current_version)
        db = DataBaseManager(DATABASE_PATH)

        _require_success(db.upgrade(), "数据库升级")
        _require_success(config_manager.update_config_file(), "配置文件升级")

        exe_manager = ExeUpgradeManager(
            logger,
            config_manager.upgrade_version_list,
        )
        _require_success(exe_manager.run(), "可执行程序升级")

        result_program = update_version_program(logger)[0]
        _require_success(result_program, "主程序升级")

        update_manager = DataBaseManager(get_version_database_path())
        _require_success(update_manager.update_version(), "数据库版本持久化")
    except Exception as e:
        _log_safely(logger, "error", f"升级流程失败: {e}")
        _restore_after_failure(logger, config_manager)
        return False

    _print_safely("更新成功!")
    _print_safely("删除备份数据库")
    try:
        _cleanup_database_backup(logger)
    except Exception as e:
        _log_safely(logger, "warning", f"数据库备份清理异常: {e}")
    _print_safely("删除备份配置文件")
    try:
        _cleanup_config_backups(logger, config_manager)
    except Exception as e:
        _log_safely(logger, "warning", f"配置备份清理异常: {e}")
    return True


def _run_cli():
    upgrade_succeeded = run_upgrade()
    os.system("pause")
    sys.exit(0 if upgrade_succeeded else 1)


if __name__ == "__main__":
    _run_cli()
