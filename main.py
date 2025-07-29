import os
import sys
import shutil
import sqlite3

from control.upgrade_db import DataBaseManager
from log_model.log_manager import LogManager
from consts.program_config import DEFAULT_DIR, DEFAULT_TARGET_DIR, DATABASE_PATH, BACKUP_DIR


def update_version_program(logger):       # local_program_path: 异音软件可执行程序位置     targer_program_path：目标版本可执行程序位置
    try:
        print("正在更新主程序...")
        logger.info("正在更新主程序...")
        targer_program_path = DEFAULT_DIR + "resources/DiTing.exe"
        local_program_path = DEFAULT_TARGET_DIR + "DiTing.exe"
        shutil.copy2(targer_program_path, local_program_path)
        print("更新主程序成功")
        logger.info("更新主程序成功")
        return True, "更新主程序成功"
    except Exception as e:
        logger.error(f"更新主程序失败: {e}")
        return False, f"更新主程序失败: {e}"
    

def get_current_version():
    try:
        conn = sqlite3.connect(DATABASE_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM system_info")
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        if result:
            return result[1]
        else:
            return None
    except Exception as e:
        print(f"获取当前版本失败: {e}")

    

if __name__ == '__main__':
    logger = LogManager.set_log_handler("db_core")
    result_backup = DataBaseManager.backup_db(logger)
    current_version = get_current_version()
    print(f"当前版本为: {current_version}",11111111111)
    if not result_backup:
        os.system("pause")
        sys.exit(1)
    print(DATABASE_PATH)
    db = DataBaseManager(DATABASE_PATH,logger)
    result_db = db.upgrade()
    result_ai = True
    result_ui = True
    result_program = False
    if result_db and result_ai and result_ui:
        result_program = update_version_program(logger)[0]
    if result_program:
        print("更新成功，删除备份数据库")
        backup_path = os.path.join(BACKUP_DIR, "audio_data_backup.db")

        if os.path.exists(backup_path):
            os.remove(backup_path)
            update_manager = DataBaseManager(DATABASE_PATH,logger)
            update_manager.update_version()
            os.system("pause")
            sys.exit(0)
    else:
        print("更新失败，用备份的数据库替换")
        DataBaseManager.restore_db(logger)
        os.system("pause")
