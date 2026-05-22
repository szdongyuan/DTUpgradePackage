import sqlite3

from base.log_manager import LogManager
from consts import error_code


class DataSave(object):
    """Minimal schema helper kept only for DB upgrade workflows."""

    def __init__(self, db_name):
        self.db_name = db_name
        self.connection = None
        self.cursor = None
        self.logger = LogManager.set_log_handler("db_core")

    def _connect_for_schema_setup(self):
        self.connection = sqlite3.connect(self.db_name)
        self.connection.execute("PRAGMA foreign_keys = ON;")
        self.cursor = self.connection.cursor()

    def _create_audio_tables(self):
        create_audio_data_table_sql = """
        CREATE TABLE IF NOT EXISTS audio_data_table(
            audio_data_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL UNIQUE,
            product_model TEXT NOT NULL,
            sample_rate INTEGER NOT NULL CHECK (sample_rate > 0),
            record_date DATETIME NOT NULL,
            labels TEXT,
            barcode TEXT,
            stimulus_id TEXT,
            FOREIGN KEY (stimulus_id) REFERENCES stimulus_signal_table (stimulus_id) ON DELETE NO ACTION ON UPDATE NO ACTION
        );
        """
        create_stimulus_signal_table_sql = """
        CREATE TABLE IF NOT EXISTS stimulus_signal_table(
            stimulus_id TEXT PRIMARY KEY,
            stimulus_method TEXT NOT NULL,
            stimulus_type TEXT NOT NULL,
            repeat_times INTEGER NOT NULL CHECK (repeat_times > 0),
            start_freq INTEGER DEFAULT NULL CHECK (start_freq >= 10),
            stop_freq INTEGER DEFAULT NULL CHECK (stop_freq >= 10 AND stop_freq <= 24000),
            sample_rate INTEGER NOT NULL CHECK (sample_rate > 0),
            total_time INTEGER NOT NULL CHECK (total_time > 0),
            num_steps INTEGER DEFAULT NULL CHECK (num_steps >= 0),
            voltage_type TEXT NOT NULL DEFAULT 'RMS',
            voltage REAL NOT NULL DEFAULT 1.0,
            is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),
            stimulus_name TEXT
        );
        """
        create_training_model_table_sql = """
        CREATE TABLE IF NOT EXISTS training_model_table(
            model_id TEXT PRIMARY KEY,
            model_name TEXT NOT NULL UNIQUE,
            model_path TEXT NOT NULL UNIQUE,
            config_path TEXT NOT NULL,
            input_dim TEXT NOT NULL,
            output_dim INTEGER NOT NULL,
            accuracy REAL CHECK (accuracy >= 0 and accuracy <= 1),
            update_date DATETIME NOT NULL,
            model_description TEXT
        );
        """
        self.cursor.execute(create_stimulus_signal_table_sql)
        self.cursor.execute(create_audio_data_table_sql)
        self.cursor.execute(create_training_model_table_sql)
        self._ensure_stimulus_voltage_columns()

    def _create_system_tables(self):
        create_users_table_sql = """
        CREATE TABLE IF NOT EXISTS users_table(
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            access_level TEXT NOT NULL CHECK(access_level IN ('Admin', 'Engineer', 'Operator')),
            user_created_time TEXT DEFAULT (DATETIME('now', '+8 hours')),
            user_updated_time TEXT DEFAULT (DATETIME('now', '+8 hours'))
        )
        """
        create_system_info_table_sql = """
        CREATE TABLE IF NOT EXISTS system_info_table(
            name TEXT PRIMARY KEY,
            value TEXT
        )
        """
        create_insert_trigger_sql = """
        CREATE TRIGGER IF NOT EXISTS ensure_one_admin_user_insert
        BEFORE INSERT ON users_table
        FOR EACH ROW
        WHEN NEW.access_level = 'Admin'
        BEGIN
            SELECT
                CASE
                    WHEN (SELECT COUNT(*) FROM users_table WHERE access_level = 'Admin') > 0
                    THEN RAISE(ABORT, 'There can only be one Admin user.')
                END;
        END;
        """
        create_update_trigger_sql = """
        CREATE TRIGGER IF NOT EXISTS ensure_one_admin_user_update
        BEFORE UPDATE ON users_table
        FOR EACH ROW
        WHEN NEW.access_level = 'Admin' AND OLD.access_level != 'Admin'
        BEGIN
            SELECT
                CASE
                    WHEN (SELECT COUNT(*) FROM users_table WHERE access_level = 'Admin') > 0
                    THEN RAISE(ABORT, 'There can only be one Admin user.')
                END;
        END;
        """
        self.cursor.execute(create_users_table_sql)
        self.cursor.execute(create_system_info_table_sql)
        self.cursor.execute(create_insert_trigger_sql)
        self.cursor.execute(create_update_trigger_sql)

    def create_audio_tables(self):
        try:
            self._connect_for_schema_setup()
            self._create_audio_tables()
            self.connection.commit()
            self.logger.info("Audio table creation success.")
            return error_code.OK, "Audio table creation success."
        except Exception as exc:
            err_msg = "Failed to create audio tables. %s" % (str(exc)[:120])
            self.logger.error(err_msg)
            return error_code.INVALID_CREATE_TABLE, err_msg

    def create_system_tables(self):
        try:
            self._connect_for_schema_setup()
            self._create_system_tables()
            self.connection.commit()
            self.logger.info("System table and trigger creation success.")
            return error_code.OK, "System table creation success."
        except Exception as exc:
            err_msg = "Failed to create system tables. %s" % (str(exc)[:120])
            self.logger.error(err_msg)
            return error_code.INVALID_CREATE_TABLE, err_msg

    def _ensure_stimulus_voltage_columns(self):
        self.cursor.execute("PRAGMA table_info(stimulus_signal_table)")
        existing_columns = {row[1] for row in self.cursor.fetchall()}
        if "voltage_type" not in existing_columns:
            self.cursor.execute(
                "ALTER TABLE stimulus_signal_table ADD COLUMN voltage_type TEXT NOT NULL DEFAULT 'RMS'"
            )
        if "voltage" not in existing_columns:
            self.cursor.execute(
                "ALTER TABLE stimulus_signal_table ADD COLUMN voltage REAL NOT NULL DEFAULT 1.0"
            )

    def close(self):
        try:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            return error_code.OK, "Database connection closed."
        except Exception as exc:
            err_msg = "Error closing the connection. %s" % (str(exc)[:120])
            self.logger.error(err_msg)
            return error_code.INVALID_CLOSED, err_msg
