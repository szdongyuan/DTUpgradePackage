import datetime
import os
import shutil
import sqlite3
from pathlib import Path

import yaml

from consts.program_config import DATABASE_PATH, SYSTEM_DATABASE_PATH, BACKUP_DIR, DEFAULT_DIR, TARGET_VERSION, ALL_VERSIONS
from log_model.log_manager import LogManager


_CURRENT_VERSION_UNSET = object()


def read_current_version_from_db(db_path):
    if not os.path.exists(db_path):
        return None
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute("SELECT value FROM system_info_table WHERE name = 'current_version';")
        result = cur.fetchone()
        return result[0] if result else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def get_version_database_path():
    if os.path.lexists(SYSTEM_DATABASE_PATH):
        return SYSTEM_DATABASE_PATH
    return DATABASE_PATH


class DataBaseManager:
    def __init__(self, db_path):
        self.logger = LogManager.set_log_handler("db_core")
        self.db_path = db_path
        if not os.path.exists(self.db_path):
            self.logger.error(f"数据库文件不存在: {self.db_path}")
            raise FileNotFoundError(f"数据库文件不存在: {self.db_path}")
        self.conn = sqlite3.connect(self.db_path)
        self.modify_table_set = set()

    @staticmethod
    def backup_db():
        logger = LogManager.set_log_handler("db_core")
        backup_path = os.path.join(BACKUP_DIR, "audio_data_backup.db")
        if not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR)
        try:
            shutil.copy(DATABASE_PATH, backup_path)
            logger.info(f"数据库备份已保存到 {backup_path}")
            print(f"数据库备份已保存到 {backup_path}")
            return True
        except Exception as e:
            print(f"备份数据库时发生错误：{e}")
            logger.error(f"备份数据库时发生错误：{e}")
            return False

    @staticmethod
    def restore_db():
        logger = LogManager.set_log_handler("db_core")
        backup_path = os.path.join(BACKUP_DIR, "audio_data_backup.db")
        if os.path.exists(backup_path):
            shutil.copy(backup_path, DATABASE_PATH)
            print(f"数据库已恢复为备份版本: {backup_path}")
            logger.info(f"数据库已恢复为备份版本: {backup_path}")
        else:
            print("未找到备份文件，无法恢复数据库。")
            logger.error("未找到备份文件，无法恢复数据库。")

    def read_update_config(self, config_file_path):
        if not os.path.exists(config_file_path):
            self.logger.error(f"找不到 YAML 文件: {config_file_path}")
            raise FileNotFoundError(f"找不到 YAML 文件: {config_file_path}")
        with open(config_file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        sql_flag = False
        if "db" in data:
            if "sqls_operate" in data["db"]:
                sql_flag = True
        if sql_flag is False:
            return sql_flag, []
        sqls = data.get("db", {}).get("sqls_operate", [])
        modify_tables = data.get("db", {}).get("data_check", {}).get("add", [])
        del_modify_tables = data.get("db", {}).get("data_check", {}).get("del", [])

        if modify_tables:
            for modify_table in modify_tables:
                self.modify_table_set.add(modify_table)
        if del_modify_tables:
            for del_modify_table in del_modify_tables:
                self.modify_table_set.discard(del_modify_table)
        return sql_flag, sqls

    def update_db(self, target_version):
        target_version_path = DEFAULT_DIR + f"resources/version_{target_version}.yml"
        sql_flag, sqls = self.read_update_config(target_version_path)
        if not sql_flag:
            self.logger.error("YAML 文件中未找到 sqls 字段")
            raise ValueError("YAML 文件中未找到 sqls 字段")
        print(f"从 {target_version_path} 读取到 {len(sqls)} 条 SQL，开始执行...")

        if not sqls:
            self.logger.info("无更新SQL")
            print("无更新SQL")
            return True

        success_count = 0
        try:
            self.conn.execute("BEGIN")
            self.conn.execute("PRAGMA foreign_keys = ON;")
            for sql in sqls:
                self.logger.info(f"执行SQL: {sql}")
                print(f"执行SQL: {sql}")
                self.conn.execute(sql)
                success_count += 1
            if success_count == len(sqls):
                print(f"版本已进行到 {target_version}")
                self.logger.info(f"版本已进行到 {target_version}")
            else:
                raise RuntimeError(f"执行 SQL 数量不匹配：期望 {len(sqls)} 条，实际成功 {success_count} 条")
        except Exception as e:
            print(f"SQL 执行失败：{e}")
            self.logger.error(f"SQL 执行失败：{e}")
            self.conn.rollback()
            print("事务已回滚。")
            self.logger.error("事务已回滚。")
            raise

    def get_current_version(self):
        # 查询当前版本
        try:
            cur = self.conn.execute("SELECT value FROM system_info_table WHERE name = 'current_version';")
            result = cur.fetchone()
        except sqlite3.Error:
            self._report_safely("查询报错", "error")
            return None
        if result:
            return result[0]
        self._report_safely("无版本号", "error")
        return None

    def _report_safely(self, message, log_method):
        try:
            print(message)
        except Exception:
            pass
        try:
            getattr(self.logger, log_method)(message)
        except Exception:
            pass

    def create_system_info_table(self):
        create_table_sql = """
            CREATE TABLE IF NOT EXISTS system_info_table (
                name TEXT PRIMARY KEY,
                value TEXT
            );
            """
        self.conn.execute(create_table_sql)
        now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        insert_sql = """
            INSERT INTO system_info_table (name, value)
            VALUES
                ('current_version', ?),
                ('last_running_version',null),
                ('update_time', ?);
            """
        init_version = "0.12"
        self.conn.execute(insert_sql, (init_version, now_time))
        self.conn.commit()
        return init_version

    def check_table_data(self):
        backup_database_path = os.path.join(BACKUP_DIR, "audio_data_backup.db")
        backup_connection = None
        result = False
        try:
            backup_uri = Path(backup_database_path).resolve().as_uri()
            backup_connection = sqlite3.connect(
                f"{backup_uri}?mode=ro",
                uri=True,
            )
            backup_connection.execute("PRAGMA schema_version;").fetchone()
            result = True
            for item in self.modify_table_set:
                original_data = backup_connection.execute(item).fetchall()
                current_data = self.conn.execute(item).fetchall()
                original_data_set = set({item[0] for item in original_data})
                current_data_set = set({item[0] for item in current_data})
                if original_data_set != current_data_set:
                    self.logger.error(f"{item} 表数据不一致！")
                    print(f"{item} 表数据不一致！")
                    result = False
                    break
                print(f"{item} 表数据一致，无需处理！")
        except Exception as exc:
            self._report_safely(f"检查备份数据库失败: {exc}", "error")
            result = False
        finally:
            if backup_connection is not None:
                try:
                    backup_connection.close()
                except Exception as exc:
                    self._report_safely(f"关闭备份数据库失败: {exc}", "error")
                    result = False
        return result

    def upgrade(self, current_version=_CURRENT_VERSION_UNSET):
        try:
            if current_version is _CURRENT_VERSION_UNSET:
                db_current_version = self.get_current_version()
                if db_current_version is None:
                    db_current_version = self.create_system_info_table()
            else:
                db_current_version = current_version
                if (
                    not isinstance(db_current_version, str)
                    or db_current_version not in ALL_VERSIONS
                ):
                    raise ValueError(
                        f"无效的显式数据库版本: {db_current_version}"
                    )
            print(f"当前数据库版本: {db_current_version}")
            self.logger.info(f"当前数据库版本: {db_current_version}")
            # 需要更新的版本列表
            start_idx = ALL_VERSIONS.index(db_current_version) + 1
            end_idx = ALL_VERSIONS.index(TARGET_VERSION) + 1
            update_versions = ALL_VERSIONS[start_idx:end_idx]
            print(f"需要执行的更新版本序列: {update_versions}")
            self.logger.info(f"需要执行的更新版本序列: {update_versions}")
            if update_versions:
                for i in update_versions:
                    self.update_db(i)
                    self.conn.commit()
            else:
                print("无需更新，数据库已是目标版本或更高版本。")
                self.logger.info("无需更新，数据库已是目标版本或更高版本。")
            if not self.check_table_data():
                raise Exception("数据库更新错误")
            return True
        except Exception as e:
            print(e)
            return False
        finally:
            self.conn.close()
            print("数据库连接已关闭")
            self.logger.info("数据库连接已关闭")

    def update_version(self):
        committed = False
        try:
            db_current_version = self.get_current_version()
            if db_current_version is None:
                raise sqlite3.OperationalError("current version is unavailable")
            now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor = self.conn.execute(
                "UPDATE system_info_table SET value = ? WHERE name = 'last_running_version';",
                (db_current_version,),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError("last_running_version update did not affect exactly one row")
            cursor = self.conn.execute(
                "UPDATE system_info_table SET value = ? WHERE name = 'current_version';",
                (TARGET_VERSION,),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError("current_version update did not affect exactly one row")
            cursor = self.conn.execute(
                "UPDATE system_info_table SET value = ? WHERE name = 'update_time';",
                (now_time,),
            )
            if cursor.rowcount != 1:
                raise sqlite3.OperationalError("update_time update did not affect exactly one row")
            self.conn.commit()
            committed = True
        except sqlite3.Error as exc:
            try:
                self.conn.rollback()
            except Exception as rollback_exc:
                self._report_safely(f"数据库版本信息事务回滚失败: {rollback_exc}", "error")
            self._report_safely(f"更新数据库版本信息失败: {exc}", "error")
        finally:
            try:
                self.conn.close()
            except Exception as close_exc:
                self._report_safely(f"关闭数据库版本信息连接失败: {close_exc}", "error")

        if committed:
            self._report_safely(f"数据库版本信息已更新到 {TARGET_VERSION}", "info")
        return committed
