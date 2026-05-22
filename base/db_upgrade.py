import logging
import os
import shutil
import sqlite3
from datetime import datetime

from base.db_manager import DataSave
from consts import error_code, model_consts


STATUS_SUCCESS = "success"
STATUS_ALREADY_UPGRADED = "already_upgraded"
STATUS_FAILED = "failed"

DETECTION_LEGACY_SINGLE = "legacy_single"
DETECTION_ALREADY_UPGRADED = STATUS_ALREADY_UPGRADED
DETECTION_MISSING_SOURCE = "missing_source"
DETECTION_INVALID = "invalid"

AUDIO_TABLES = {"audio_data_table", "stimulus_signal_table", "training_model_table"}
SYSTEM_TABLES = {"users_table", "system_info_table"}
ALL_SUPPORTED_TABLES = AUDIO_TABLES | SYSTEM_TABLES

TABLE_COPY_CONFIG = {
    "stimulus_signal_table": {
        "destination_columns": model_consts.DB_STIMULUS_COLUMNS,
        "required_columns": {
            "stimulus_id",
            "stimulus_method",
            "stimulus_type",
            "repeat_times",
            "sample_rate",
            "total_time",
            "is_default",
        },
        "default_values": {
            "start_freq": None,
            "stop_freq": None,
            "num_steps": None,
            "voltage_type": "RMS",
            "voltage": 1.0,
            "stimulus_name": None,
        },
    },
    "audio_data_table": {
        "destination_columns": model_consts.DB_AUDIO_COLUMNS,
        "required_columns": {
            "audio_data_id",
            "file_path",
            "product_model",
            "sample_rate",
            "record_date",
        },
        "default_values": {
            "labels": None,
            "barcode": None,
            "stimulus_id": None,
        },
    },
    "training_model_table": {
        "destination_columns": model_consts.DB_MODEL_COLUMNS,
        "required_columns": {
            "model_id",
            "model_name",
            "model_path",
            "config_path",
            "input_dim",
            "output_dim",
            "update_date",
        },
        "default_values": {
            "accuracy": None,
            "model_description": None,
        },
    },
    "users_table": {
        "destination_columns": model_consts.DB_USERS_COLUMNS,
        "required_columns": {
            "user_id",
            "user_name",
            "password",
            "access_level",
        },
        "default_values": {
            "user_created_time": None,
            "user_updated_time": None,
        },
    },
    "system_info_table": {
        "destination_columns": ["name", "value"],
        "required_columns": {"name"},
        "default_values": {"value": None},
    },
}

LOGGER = logging.getLogger(__name__)


def _get_database_paths(database_dir=None):
    if database_dir is None:
        database_dir = os.path.dirname(model_consts.AUDIO_DATABASE_PATH)
    return {
        "database_dir": database_dir,
        "audio_path": os.path.join(database_dir, "audio_data.db"),
        "system_path": os.path.join(database_dir, "system_data.db"),
    }


def _get_user_table_names(db_path):
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in cursor.fetchall() if not row[0].startswith("sqlite_")}
    finally:
        connection.close()


def _is_valid_system_database(db_path):
    if not os.path.exists(db_path):
        return False
    connection = sqlite3.connect(db_path)
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        tables = {row[0] for row in cursor.fetchall() if not row[0].startswith("sqlite_")}
        if not SYSTEM_TABLES.issubset(tables):
            return False
        cursor.execute("SELECT value FROM system_info_table WHERE name = ?", ("current_version",))
        return cursor.fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def _is_valid_audio_database(db_path):
    if not os.path.exists(db_path):
        return False
    try:
        tables = _get_user_table_names(db_path)
    except sqlite3.Error:
        return False
    return AUDIO_TABLES.issubset(tables) and not bool(SYSTEM_TABLES & tables)


def detect_legacy_database_state(database_dir=None):
    paths = _get_database_paths(database_dir)
    audio_path = paths["audio_path"]
    system_path = paths["system_path"]

    if not os.path.exists(audio_path):
        return DETECTION_MISSING_SOURCE, f"Missing legacy database: {audio_path}", paths

    audio_tables = _get_user_table_names(audio_path)
    if _is_valid_audio_database(audio_path) and _is_valid_system_database(system_path):
        return DETECTION_ALREADY_UPGRADED, "The databases are already in split format.", paths

    if ALL_SUPPORTED_TABLES.issubset(audio_tables):
        extra_tables = sorted(audio_tables - ALL_SUPPORTED_TABLES)
        if extra_tables:
            return (
                DETECTION_INVALID,
                f"Legacy database contains unsupported tables: {', '.join(extra_tables)}",
                paths,
            )
        return DETECTION_LEGACY_SINGLE, "Legacy single database detected.", paths

    missing_tables = sorted(ALL_SUPPORTED_TABLES - audio_tables)
    if missing_tables:
        return (
            DETECTION_INVALID,
            f"Database is not a supported legacy single database, missing tables: {', '.join(missing_tables)}",
            paths,
        )

    return DETECTION_INVALID, "Database layout is not supported for upgrade.", paths


def _get_source_columns(connection, table_name):
    cursor = connection.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cursor.fetchall()]


def _count_rows(connection, table_name):
    cursor = connection.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
    return cursor.fetchone()[0]


def _validate_source_constraints(source_connection):
    cursor = source_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM users_table WHERE access_level = ?", ("Admin",))
    admin_count = cursor.fetchone()[0]
    if admin_count != 1:
        raise ValueError(f"Legacy users_table must contain exactly one Admin user, found {admin_count}.")

    cursor.execute("SELECT value FROM system_info_table WHERE name = ?", ("current_version",))
    if cursor.fetchone() is None:
        raise ValueError("Legacy system_info_table is missing current_version.")


def _create_destination_databases(audio_temp_path, system_temp_path):
    audio_database = DataSave(audio_temp_path)
    audio_code, audio_msg = audio_database.create_audio_tables()
    if audio_database.connection is not None:
        audio_database.close()
    if audio_code != error_code.OK:
        raise RuntimeError(audio_msg)

    system_database = DataSave(system_temp_path)
    system_code, system_msg = system_database.create_system_tables()
    if system_database.connection is not None:
        system_database.close()
    if system_code != error_code.OK:
        raise RuntimeError(system_msg)


def _build_insert_payload(rows, appended_defaults):
    if not appended_defaults:
        return rows
    suffix = tuple(appended_defaults[column] for column in appended_defaults)
    return [tuple(row) + suffix for row in rows]


def _copy_table(source_connection, destination_connection, table_name):
    config = TABLE_COPY_CONFIG[table_name]
    source_columns = _get_source_columns(source_connection, table_name)
    missing_required = sorted(config["required_columns"] - set(source_columns))
    if missing_required:
        raise ValueError(f"{table_name} is missing required columns: {', '.join(missing_required)}")

    shared_columns = [column for column in config["destination_columns"] if column in source_columns]
    appended_defaults = {
        column: default
        for column, default in config["default_values"].items()
        if column in config["destination_columns"] and column not in source_columns
    }
    insert_columns = shared_columns + list(appended_defaults.keys())

    cursor = source_connection.cursor()
    if shared_columns:
        cursor.execute(f"SELECT {', '.join(shared_columns)} FROM {table_name}")
        rows = cursor.fetchall()
    else:
        cursor.execute(f"SELECT COUNT(*) FROM {table_name}")
        rows = [tuple() for _ in range(cursor.fetchone()[0])]

    insert_rows = _build_insert_payload(rows, appended_defaults)
    if insert_rows:
        placeholders = ", ".join(["?"] * len(insert_columns))
        destination_connection.executemany(
            f"INSERT INTO {table_name} ({', '.join(insert_columns)}) VALUES ({placeholders})",
            insert_rows,
        )


def _sync_users_autoincrement(system_connection):
    cursor = system_connection.cursor()
    cursor.execute("SELECT MAX(user_id) FROM users_table")
    max_user_id = cursor.fetchone()[0]
    if max_user_id is None:
        return
    try:
        cursor.execute("SELECT COUNT(*) FROM sqlite_sequence WHERE name = ?", ("users_table",))
    except sqlite3.OperationalError:
        return
    if cursor.fetchone()[0]:
        cursor.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (max_user_id, "users_table"))
    else:
        cursor.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", ("users_table", max_user_id))


def _validate_upgraded_databases(source_path, audio_path, system_path):
    if not _is_valid_audio_database(audio_path):
        raise ValueError("Migrated audio_data.db failed schema validation.")
    if not _is_valid_system_database(system_path):
        raise ValueError("Migrated system_data.db failed schema validation.")

    source_connection = sqlite3.connect(source_path)
    audio_connection = sqlite3.connect(audio_path)
    system_connection = sqlite3.connect(system_path)
    try:
        for table_name in ("stimulus_signal_table", "audio_data_table", "training_model_table"):
            if _count_rows(source_connection, table_name) != _count_rows(audio_connection, table_name):
                raise ValueError(f"Row count mismatch after migrating {table_name}.")
        for table_name in ("users_table", "system_info_table"):
            if _count_rows(source_connection, table_name) != _count_rows(system_connection, table_name):
                raise ValueError(f"Row count mismatch after migrating {table_name}.")

        cursor = audio_connection.cursor()
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM audio_data_table
            WHERE stimulus_id IS NOT NULL
              AND stimulus_id NOT IN (SELECT stimulus_id FROM stimulus_signal_table)
            """
        )
        if cursor.fetchone()[0] != 0:
            raise ValueError("Migrated audio_data_table contains invalid stimulus_id references.")
    finally:
        source_connection.close()
        audio_connection.close()
        system_connection.close()


def _make_timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def upgrade_legacy_single_database(database_dir=None):
    try:
        detection_status, detection_message, paths = detect_legacy_database_state(database_dir)
        if detection_status == DETECTION_ALREADY_UPGRADED:
            return STATUS_ALREADY_UPGRADED, detection_message
        if detection_status != DETECTION_LEGACY_SINGLE:
            return STATUS_FAILED, detection_message

        database_dir = paths["database_dir"]
        audio_path = paths["audio_path"]
        system_path = paths["system_path"]
        timestamp = _make_timestamp()
        backup_dir = os.path.join(database_dir, "backup")
        backup_path = os.path.join(backup_dir, f"audio_data_legacy_backup_{timestamp}.db")
        audio_temp_path = os.path.join(database_dir, f"audio_data.db.tmp.{timestamp}")
        system_temp_path = os.path.join(database_dir, f"system_data.db.tmp.{timestamp}")
        system_restore_path = os.path.join(database_dir, f"system_data.db.pre_upgrade.{timestamp}")

        os.makedirs(database_dir, exist_ok=True)
        os.makedirs(backup_dir, exist_ok=True)
        shutil.copy2(audio_path, backup_path)
        if not os.path.exists(backup_path) or os.path.getsize(backup_path) == 0:
            raise RuntimeError("Failed to create a valid legacy database backup.")

        for temp_path in (audio_temp_path, system_temp_path):
            if os.path.exists(temp_path):
                os.remove(temp_path)

        _create_destination_databases(audio_temp_path, system_temp_path)

        source_connection = sqlite3.connect(audio_path)
        audio_connection = sqlite3.connect(audio_temp_path)
        system_connection = sqlite3.connect(system_temp_path)
        try:
            audio_connection.execute("PRAGMA foreign_keys = ON;")
            system_connection.execute("PRAGMA foreign_keys = ON;")
            _validate_source_constraints(source_connection)

            _copy_table(source_connection, audio_connection, "stimulus_signal_table")
            _copy_table(source_connection, audio_connection, "audio_data_table")
            _copy_table(source_connection, audio_connection, "training_model_table")
            audio_connection.commit()

            _copy_table(source_connection, system_connection, "users_table")
            _copy_table(source_connection, system_connection, "system_info_table")
            _sync_users_autoincrement(system_connection)
            system_connection.commit()
        finally:
            source_connection.close()
            audio_connection.close()
            system_connection.close()

        _validate_upgraded_databases(audio_path, audio_temp_path, system_temp_path)

        if os.path.exists(system_restore_path):
            os.remove(system_restore_path)
        if os.path.exists(system_path):
            os.replace(system_path, system_restore_path)

        try:
            os.replace(audio_temp_path, audio_path)
            os.replace(system_temp_path, system_path)
        except Exception:
            if os.path.exists(audio_path):
                os.remove(audio_path)
            shutil.copy2(backup_path, audio_path)
            if os.path.exists(system_restore_path):
                if os.path.exists(system_path):
                    os.remove(system_path)
                os.replace(system_restore_path, system_path)
            raise

        if os.path.exists(system_restore_path):
            os.remove(system_restore_path)

        return STATUS_SUCCESS, (
            "Database upgrade completed successfully. "
            f"Backup saved to: {backup_path}"
        )
    except Exception as exc:
        LOGGER.exception("Database upgrade failed.")
        return STATUS_FAILED, f"Database upgrade failed: {exc}"
