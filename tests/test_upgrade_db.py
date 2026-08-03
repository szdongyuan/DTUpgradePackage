import os
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

from control import upgrade_db
from control.upgrade_db import DataBaseManager


class DummyLogger:
    def info(self, message):
        pass

    def error(self, message):
        pass


class RaisingInfoLogger(DummyLogger):
    def info(self, message):
        raise RuntimeError("logger failed after commit")


class RaisingErrorLogger(DummyLogger):
    def error(self, message):
        raise RuntimeError("logger failed while reporting transaction failure")


class ConnectionProxy:
    def __init__(
        self,
        connection,
        fail_select=False,
        fail_update=None,
        fail_commit=False,
        fail_rollback=False,
        fail_close=False,
    ):
        self.connection = connection
        self.fail_select = fail_select
        self.fail_update = fail_update
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.fail_close = fail_close
        self.rollback_called = False
        self.closed = False

    def execute(self, sql, parameters=()):
        if self.fail_select and sql.lstrip().upper().startswith("SELECT"):
            raise sqlite3.OperationalError("simulated select failure")
        if (
            self.fail_update
            and sql.lstrip().upper().startswith("UPDATE")
            and f"name = '{self.fail_update}'" in sql
        ):
            raise sqlite3.OperationalError("simulated update failure")
        return self.connection.execute(sql, parameters)

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("simulated commit failure")
        return self.connection.commit()

    def rollback(self):
        self.rollback_called = True
        result = self.connection.rollback()
        if self.fail_rollback:
            raise sqlite3.OperationalError("simulated rollback failure")
        return result

    def close(self):
        self.closed = True
        result = self.connection.close()
        if self.fail_close:
            raise sqlite3.OperationalError("simulated close failure")
        return result


@pytest.fixture
def local_tmp_path():
    path = Path(__file__).parent / ".tmp" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def create_version_db(path, version, include_current_version=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE system_info_table (name TEXT PRIMARY KEY, value TEXT)")
        if include_current_version:
            conn.execute("INSERT INTO system_info_table (name, value) VALUES ('current_version', ?)", (version,))
        conn.execute(
            "INSERT INTO system_info_table (name, value) VALUES ('last_running_version', ?)",
            ("previous-last-running",),
        )
        conn.execute(
            "INSERT INTO system_info_table (name, value) VALUES ('update_time', ?)",
            ("2000-01-01 00:00:00",),
        )
        conn.commit()
    finally:
        conn.close()


def read_version_value(path, name):
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM system_info_table WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def read_version_metadata(path):
    conn = sqlite3.connect(path)
    try:
        return dict(
            conn.execute(
                """
                SELECT name, value
                FROM system_info_table
                WHERE name IN ('last_running_version', 'current_version', 'update_time')
                """
            ).fetchall()
        )
    finally:
        conn.close()


def delete_version_metadata(path, name):
    conn = sqlite3.connect(path)
    try:
        conn.execute("DELETE FROM system_info_table WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


def read_table_names(path):
    conn = sqlite3.connect(path)
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()


def create_comparison_db(path, row_ids):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        conn.executemany(
            "INSERT INTO records (id, value) VALUES (?, ?)",
            [(row_id, f"value-{row_id}") for row_id in row_ids],
        )
        conn.commit()
    finally:
        conn.close()


def test_check_table_data_accepts_matching_read_only_backup(
    monkeypatch,
    local_tmp_path,
):
    database_path = local_tmp_path / "audio_data.db"
    backup_dir = local_tmp_path / "backup"
    backup_path = backup_dir / "audio_data_backup.db"
    create_comparison_db(database_path, [1, 2])
    create_comparison_db(backup_path, [2, 1])
    monkeypatch.setattr(upgrade_db, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    manager.modify_table_set.add("SELECT id FROM records")

    assert manager.check_table_data() is True

    manager.conn.close()


def test_check_table_data_missing_backup_fails_without_creating_file(
    monkeypatch,
    local_tmp_path,
):
    database_path = local_tmp_path / "audio_data.db"
    backup_dir = local_tmp_path / "backup"
    backup_dir.mkdir()
    backup_path = backup_dir / "audio_data_backup.db"
    create_comparison_db(database_path, [1])
    monkeypatch.setattr(upgrade_db, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    manager.modify_table_set.add("SELECT id FROM records")

    assert manager.check_table_data() is False
    assert not backup_path.exists()

    manager.conn.close()


@pytest.mark.parametrize("failure", ["corrupt", "query"])
def test_check_table_data_rejects_corrupt_backup_or_query_failure(
    monkeypatch,
    local_tmp_path,
    failure,
):
    database_path = local_tmp_path / "audio_data.db"
    backup_dir = local_tmp_path / "backup"
    backup_path = backup_dir / "audio_data_backup.db"
    create_comparison_db(database_path, [1])
    if failure == "corrupt":
        backup_dir.mkdir()
        backup_path.write_bytes(b"not a sqlite database")
        comparison_sql = "SELECT id FROM records"
    else:
        create_comparison_db(backup_path, [1])
        comparison_sql = "SELECT id FROM missing_table"
    monkeypatch.setattr(upgrade_db, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    manager.modify_table_set.add(comparison_sql)

    assert manager.check_table_data() is False

    manager.conn.close()


def test_quick_check_rejects_parseable_database_with_corrupt_data_page(
    monkeypatch,
    local_tmp_path,
):
    database_path = local_tmp_path / "audio_data.db"
    backup_dir = local_tmp_path / "backup"
    backup_path = backup_dir / "audio_data_backup.db"
    create_comparison_db(database_path, [1])
    backup_dir.mkdir()
    backup_connection = sqlite3.connect(backup_path)
    try:
        backup_connection.execute("PRAGMA page_size = 512")
        backup_connection.execute(
            "CREATE TABLE damaged (id INTEGER PRIMARY KEY, payload BLOB)"
        )
        backup_connection.executemany(
            "INSERT INTO damaged (payload) VALUES (?)",
            [(bytes([index % 251]) * 400,) for index in range(100)],
        )
        backup_connection.commit()
        page_size = backup_connection.execute(
            "PRAGMA page_size"
        ).fetchone()[0]
        page_count = backup_connection.execute(
            "PRAGMA page_count"
        ).fetchone()[0]
        root_page = backup_connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 'damaged'"
        ).fetchone()[0]
    finally:
        backup_connection.close()
    assert page_count > root_page
    with backup_path.open("r+b") as backup_file:
        backup_file.seek((page_count - 1) * page_size)
        backup_file.write(b"\x00" * page_size)

    readonly_uri = f"{backup_path.resolve().as_uri()}?mode=ro"
    probe_connection = sqlite3.connect(readonly_uri, uri=True)
    try:
        assert probe_connection.execute(
            "PRAGMA schema_version"
        ).fetchone() == (1,)
        try:
            quick_check_rows = probe_connection.execute(
                "PRAGMA quick_check"
            ).fetchall()
        except sqlite3.DatabaseError:
            quick_check_rows = []
        assert quick_check_rows != [("ok",)]
    finally:
        probe_connection.close()

    monkeypatch.setattr(upgrade_db, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(upgrade_db, "ALL_VERSIONS", ["2.0"])
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "2.0")
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    real_connect = sqlite3.connect
    checker_connections = []

    def tracking_connect(database, *args, **kwargs):
        connection_proxy = ConnectionProxy(
            real_connect(database, *args, **kwargs)
        )
        checker_connections.append(connection_proxy)
        return connection_proxy

    with monkeypatch.context() as checker_context:
        checker_context.setattr(
            upgrade_db.sqlite3,
            "connect",
            tracking_connect,
        )

        assert manager.check_table_data() is False
        assert manager.upgrade("2.0") is False

    assert len(checker_connections) == 2
    assert all(connection.closed for connection in checker_connections)
    conn = sqlite3.connect(database_path)
    try:
        assert conn.execute("SELECT id, value FROM records").fetchall() == [
            (1, "value-1")
        ]
    finally:
        conn.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        manager.conn.execute("SELECT 1")


def test_check_table_data_rejects_mismatched_rows(
    monkeypatch,
    local_tmp_path,
):
    database_path = local_tmp_path / "audio_data.db"
    backup_dir = local_tmp_path / "backup"
    backup_path = backup_dir / "audio_data_backup.db"
    create_comparison_db(database_path, [1, 2])
    create_comparison_db(backup_path, [1, 3])
    monkeypatch.setattr(upgrade_db, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    manager.modify_table_set.add("SELECT id FROM records")

    assert manager.check_table_data() is False

    manager.conn.close()


@pytest.mark.parametrize("backup_failure", ["missing", "corrupt", "query"])
def test_upgrade_fails_when_real_backup_checker_cannot_validate(
    monkeypatch,
    local_tmp_path,
    backup_failure,
):
    database_path = local_tmp_path / "audio_data.db"
    backup_dir = local_tmp_path / "backup"
    backup_path = backup_dir / "audio_data_backup.db"
    create_comparison_db(database_path, [1])
    if backup_failure == "missing":
        backup_dir.mkdir()
    elif backup_failure == "corrupt":
        backup_dir.mkdir()
        backup_path.write_bytes(b"not a sqlite database")
    else:
        create_comparison_db(backup_path, [1])

    monkeypatch.setattr(upgrade_db, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(upgrade_db, "ALL_VERSIONS", ["2.0"])
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "2.0")
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    if backup_failure == "query":
        manager.modify_table_set.add("SELECT id FROM missing_table")

    assert manager.upgrade("2.0") is False
    if backup_failure == "missing":
        assert not backup_path.exists()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        manager.conn.execute("SELECT 1")


@pytest.mark.parametrize(
    ("outcome", "backup_ids", "comparison_sql", "expected_result"),
    [
        ("success", [1, 2], "SELECT id FROM records", True),
        ("mismatch", [1, 3], "SELECT id FROM records", False),
        ("exception", [1, 2], "SELECT id FROM missing_table", False),
    ],
)
def test_check_table_data_always_closes_backup_connection(
    monkeypatch,
    local_tmp_path,
    outcome,
    backup_ids,
    comparison_sql,
    expected_result,
):
    database_path = local_tmp_path / "audio_data.db"
    backup_dir = local_tmp_path / "backup"
    backup_path = backup_dir / "audio_data_backup.db"
    create_comparison_db(database_path, [1, 2])
    create_comparison_db(backup_path, backup_ids)
    monkeypatch.setattr(upgrade_db, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    manager.modify_table_set.add(comparison_sql)
    real_connect = sqlite3.connect
    backup_connections = []
    connect_calls = []

    def tracking_connect(database, *args, **kwargs):
        connect_calls.append((database, kwargs))
        connection_proxy = ConnectionProxy(
            real_connect(database, *args, **kwargs)
        )
        backup_connections.append(connection_proxy)
        return connection_proxy

    monkeypatch.setattr(upgrade_db.sqlite3, "connect", tracking_connect)

    assert manager.check_table_data() is expected_result, outcome
    assert len(backup_connections) == 1
    assert backup_connections[0].closed is True
    assert connect_calls[0][1]["uri"] is True
    assert "mode=ro" in str(connect_calls[0][0])

    manager.conn.close()


def test_explicit_target_version_makes_split_audio_database_rerun_a_noop(
    monkeypatch,
    local_tmp_path,
):
    database_path = local_tmp_path / "audio_data.db"
    system_database_path = local_tmp_path / "system_data.db"
    conn = sqlite3.connect(database_path)
    try:
        conn.execute(
            "CREATE TABLE migrated_data (id INTEGER PRIMARY KEY, migrated TEXT)"
        )
        conn.execute(
            "INSERT INTO migrated_data (migrated) VALUES ('preserved')"
        )
        conn.commit()
    finally:
        conn.close()
    create_version_db(system_database_path, "0.26.07.01")
    authoritative_version = upgrade_db.read_current_version_from_db(
        str(system_database_path)
    )
    package_dir = local_tmp_path / "package"
    resources_dir = package_dir / "resources"
    resources_dir.mkdir(parents=True)
    (resources_dir / "version_0.26.07.01.yml").write_text(
        "db:\n"
        "  sqls_operate:\n"
        "    - ALTER TABLE migrated_data ADD COLUMN migrated TEXT\n"
        "  data_check:\n"
        "    add: []\n"
        "    del: []\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        upgrade_db,
        "ALL_VERSIONS",
        ["0.12", "0.26.07.01"],
    )
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.07.01")
    monkeypatch.setattr(upgrade_db, "DEFAULT_DIR", str(package_dir) + os.sep)
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    backup_dir = local_tmp_path / "backup"
    backup_dir.mkdir()
    shutil.copy2(database_path, backup_dir / "audio_data_backup.db")
    monkeypatch.setattr(upgrade_db, "BACKUP_DIR", str(backup_dir))

    assert manager.upgrade(authoritative_version) is True

    assert "system_info_table" not in read_table_names(database_path)
    conn = sqlite3.connect(database_path)
    try:
        assert conn.execute(
            "SELECT migrated FROM migrated_data"
        ).fetchall() == [("preserved",)]
    finally:
        conn.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        manager.conn.execute("SELECT 1")


def test_explicit_older_version_applies_updates_to_connected_audio_database(
    monkeypatch,
    local_tmp_path,
):
    database_path = local_tmp_path / "audio_data.db"
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("CREATE TABLE audio_records (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    package_dir = local_tmp_path / "package"
    resources_dir = package_dir / "resources"
    resources_dir.mkdir(parents=True)
    (resources_dir / "version_2.0.yml").write_text(
        "db:\n"
        "  sqls_operate:\n"
        "    - ALTER TABLE audio_records ADD COLUMN migrated TEXT\n"
        "  data_check:\n"
        "    add: []\n"
        "    del: []\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(upgrade_db, "ALL_VERSIONS", ["1.0", "2.0"])
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "2.0")
    monkeypatch.setattr(upgrade_db, "DEFAULT_DIR", str(package_dir) + os.sep)
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    backup_dir = local_tmp_path / "backup"
    backup_dir.mkdir()
    shutil.copy2(database_path, backup_dir / "audio_data_backup.db")
    monkeypatch.setattr(upgrade_db, "BACKUP_DIR", str(backup_dir))

    assert manager.upgrade("1.0") is True

    conn = sqlite3.connect(database_path)
    try:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(audio_records)").fetchall()
        }
    finally:
        conn.close()
    assert columns == {"id", "migrated"}
    assert "system_info_table" not in read_table_names(database_path)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        manager.conn.execute("SELECT 1")


@pytest.mark.parametrize("current_version", [None, "", "9.9", 123])
def test_invalid_explicit_version_fails_without_database_mutation_and_closes(
    monkeypatch,
    local_tmp_path,
    current_version,
):
    database_path = local_tmp_path / "audio_data.db"
    conn = sqlite3.connect(database_path)
    try:
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker (value) VALUES ('unchanged')")
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(upgrade_db, "ALL_VERSIONS", ["1.0", "2.0"])
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "2.0")
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))

    assert manager.upgrade(current_version) is False

    assert read_table_names(database_path) == {"marker"}
    conn = sqlite3.connect(database_path)
    try:
        assert conn.execute("SELECT value FROM marker").fetchall() == [
            ("unchanged",)
        ]
    finally:
        conn.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        manager.conn.execute("SELECT 1")


def test_upgrade_without_explicit_version_preserves_legacy_initialization(
    monkeypatch,
    local_tmp_path,
):
    database_path = local_tmp_path / "audio_data.db"
    sqlite3.connect(database_path).close()
    monkeypatch.setattr(upgrade_db, "ALL_VERSIONS", ["0.12"])
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.12")
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: DummyLogger()),
    )
    manager = DataBaseManager(str(database_path))
    manager.check_table_data = lambda: True

    assert manager.upgrade() is True
    assert read_version_value(database_path, "current_version") == "0.12"


def test_version_database_path_uses_audio_database_for_legacy_single_db(monkeypatch, local_tmp_path):
    audio_path = local_tmp_path / "audio_data.db"
    system_path = local_tmp_path / "system_data.db"
    create_version_db(audio_path, "0.25.12.01")
    monkeypatch.setattr(upgrade_db, "DATABASE_PATH", str(audio_path))
    monkeypatch.setattr(upgrade_db, "SYSTEM_DATABASE_PATH", str(system_path))

    assert upgrade_db.get_version_database_path() == str(audio_path)
    assert upgrade_db.read_current_version_from_db(str(audio_path)) == "0.25.12.01"


def test_version_database_path_prefers_system_database_after_split(monkeypatch, local_tmp_path):
    audio_path = local_tmp_path / "audio_data.db"
    system_path = local_tmp_path / "system_data.db"
    audio_path.write_bytes(b"")
    create_version_db(system_path, "0.26.06.01")
    monkeypatch.setattr(upgrade_db, "DATABASE_PATH", str(audio_path))
    monkeypatch.setattr(upgrade_db, "SYSTEM_DATABASE_PATH", str(system_path))

    assert upgrade_db.get_version_database_path() == str(system_path)
    assert upgrade_db.read_current_version_from_db(str(audio_path)) is None


@pytest.mark.parametrize(
    "invalid_system_state",
    ["missing-current-version", "missing-table", "invalid-database"],
)
def test_version_database_path_selects_existing_invalid_split_database(
    monkeypatch,
    local_tmp_path,
    invalid_system_state,
):
    audio_path = local_tmp_path / "audio_data.db"
    system_path = local_tmp_path / "system_data.db"
    create_version_db(audio_path, "0.25.12.01")

    if invalid_system_state == "missing-current-version":
        create_version_db(
            system_path,
            "unused",
            include_current_version=False,
        )
    elif invalid_system_state == "missing-table":
        sqlite3.connect(system_path).close()
    else:
        system_path.write_bytes(b"not a sqlite database")

    monkeypatch.setattr(upgrade_db, "DATABASE_PATH", str(audio_path))
    monkeypatch.setattr(upgrade_db, "SYSTEM_DATABASE_PATH", str(system_path))

    assert upgrade_db.get_version_database_path() == str(system_path)


def test_version_database_path_does_not_query_existing_split_database(
    monkeypatch,
    local_tmp_path,
):
    audio_path = local_tmp_path / "audio_data.db"
    system_path = local_tmp_path / "system_data.db"
    create_version_db(audio_path, "0.25.12.01")
    system_path.write_bytes(b"unreadable contents")
    reader_calls = []

    def fail_if_queried(path):
        reader_calls.append(path)
        raise sqlite3.OperationalError("simulated selection query failure")

    monkeypatch.setattr(upgrade_db, "DATABASE_PATH", str(audio_path))
    monkeypatch.setattr(upgrade_db, "SYSTEM_DATABASE_PATH", str(system_path))
    monkeypatch.setattr(
        upgrade_db,
        "read_current_version_from_db",
        fail_if_queried,
    )

    assert upgrade_db.get_version_database_path() == str(system_path)
    assert reader_calls == []


@pytest.mark.parametrize(
    "database_name",
    ["audio_data.db", "system_data.db"],
    ids=["legacy-audio-database", "split-system-database"],
)
def test_update_version_commits_metadata_and_returns_true(monkeypatch, local_tmp_path, database_name):
    database_path = local_tmp_path / database_name
    create_version_db(database_path, "0.26.02.01")
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.06.01")
    monkeypatch.setattr(upgrade_db.LogManager, "set_log_handler", staticmethod(lambda _name: DummyLogger()))

    manager = DataBaseManager(str(database_path))

    assert manager.update_version() is True
    assert read_version_value(database_path, "last_running_version") == "0.26.02.01"
    assert read_version_value(database_path, "current_version") == "0.26.06.01"
    assert read_version_value(database_path, "update_time") != "2000-01-01 00:00:00"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        manager.conn.execute("SELECT 1")


@pytest.mark.parametrize(
    "database_name",
    ["audio_data.db", "system_data.db"],
    ids=["legacy-audio-database", "split-system-database"],
)
@pytest.mark.parametrize(
    ("failure", "include_current_version", "proxy_options"),
    [
        ("missing-current-version", False, {}),
        ("select", True, {"fail_select": True}),
        ("update", True, {"fail_update": "current_version"}),
        ("commit", True, {"fail_commit": True}),
    ],
)
def test_update_version_rolls_back_and_closes_on_transaction_failure(
    monkeypatch,
    local_tmp_path,
    database_name,
    failure,
    include_current_version,
    proxy_options,
):
    database_path = local_tmp_path / database_name
    create_version_db(database_path, "0.26.02.01", include_current_version=include_current_version)
    metadata_before = read_version_metadata(database_path)
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.06.01")
    monkeypatch.setattr(upgrade_db.LogManager, "set_log_handler", staticmethod(lambda _name: DummyLogger()))
    manager = DataBaseManager(str(database_path))
    connection_proxy = ConnectionProxy(manager.conn, **proxy_options)
    manager.conn = connection_proxy

    assert manager.update_version() is False, failure
    assert connection_proxy.rollback_called is True
    assert connection_proxy.closed is True
    assert read_version_metadata(database_path) == metadata_before


@pytest.mark.parametrize(
    "database_name",
    ["audio_data.db", "system_data.db"],
    ids=["legacy-audio-database", "split-system-database"],
)
@pytest.mark.parametrize("missing_name", ["last_running_version", "update_time"])
def test_update_version_rolls_back_when_required_metadata_row_is_missing(
    monkeypatch, local_tmp_path, database_name, missing_name
):
    database_path = local_tmp_path / database_name
    create_version_db(database_path, "0.26.02.01")
    delete_version_metadata(database_path, missing_name)
    metadata_before = read_version_metadata(database_path)
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.06.01")
    monkeypatch.setattr(upgrade_db.LogManager, "set_log_handler", staticmethod(lambda _name: DummyLogger()))
    manager = DataBaseManager(str(database_path))
    connection_proxy = ConnectionProxy(manager.conn)
    manager.conn = connection_proxy

    assert manager.update_version() is False
    assert connection_proxy.rollback_called is True
    assert connection_proxy.closed is True
    assert read_version_metadata(database_path) == metadata_before


@pytest.mark.parametrize(
    "database_name",
    ["audio_data.db", "system_data.db"],
    ids=["legacy-audio-database", "split-system-database"],
)
@pytest.mark.parametrize(
    ("failure", "include_current_version", "proxy_options"),
    [
        ("missing-current-version", False, {}),
        ("select", True, {"fail_select": True}),
        ("update", True, {"fail_update": "current_version"}),
        ("commit", True, {"fail_commit": True}),
    ],
)
def test_update_version_error_logger_failure_cannot_mask_database_failure(
    monkeypatch,
    local_tmp_path,
    database_name,
    failure,
    include_current_version,
    proxy_options,
):
    database_path = local_tmp_path / database_name
    create_version_db(database_path, "0.26.02.01", include_current_version=include_current_version)
    metadata_before = read_version_metadata(database_path)
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.06.01")
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: RaisingErrorLogger()),
    )
    manager = DataBaseManager(str(database_path))
    connection_proxy = ConnectionProxy(manager.conn, **proxy_options)
    manager.conn = connection_proxy

    assert manager.update_version() is False, failure
    assert connection_proxy.rollback_called is True
    assert connection_proxy.closed is True
    assert read_version_metadata(database_path) == metadata_before


@pytest.mark.parametrize(
    "database_name",
    ["audio_data.db", "system_data.db"],
    ids=["legacy-audio-database", "split-system-database"],
)
@pytest.mark.parametrize(
    ("failure", "include_current_version", "proxy_options"),
    [
        ("missing-current-version", False, {}),
        ("select", True, {"fail_select": True}),
    ],
)
def test_update_version_console_failure_cannot_mask_current_version_read_failure(
    monkeypatch,
    local_tmp_path,
    database_name,
    failure,
    include_current_version,
    proxy_options,
):
    database_path = local_tmp_path / database_name
    create_version_db(database_path, "0.26.02.01", include_current_version=include_current_version)
    metadata_before = read_version_metadata(database_path)
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.06.01")
    monkeypatch.setattr(upgrade_db.LogManager, "set_log_handler", staticmethod(lambda _name: DummyLogger()))
    manager = DataBaseManager(str(database_path))
    connection_proxy = ConnectionProxy(manager.conn, **proxy_options)
    manager.conn = connection_proxy

    with monkeypatch.context() as reporting_context:
        reporting_context.setattr(
            "builtins.print",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("console failed while reporting transaction failure")
            ),
        )
        result = manager.update_version()

    assert result is False, failure
    assert connection_proxy.rollback_called is True
    assert connection_proxy.closed is True
    assert read_version_metadata(database_path) == metadata_before


def test_update_version_rollback_close_and_diagnostic_failures_are_isolated(
    monkeypatch, local_tmp_path
):
    database_path = local_tmp_path / "system_data.db"
    create_version_db(database_path, "0.26.02.01")
    metadata_before = read_version_metadata(database_path)
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.06.01")
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: RaisingErrorLogger()),
    )
    manager = DataBaseManager(str(database_path))
    connection_proxy = ConnectionProxy(
        manager.conn,
        fail_commit=True,
        fail_rollback=True,
        fail_close=True,
    )
    manager.conn = connection_proxy

    with monkeypatch.context() as reporting_context:
        reporting_context.setattr(
            "builtins.print",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("console failed while reporting cleanup failure")
            ),
        )
        result = manager.update_version()

    assert result is False
    assert connection_proxy.rollback_called is True
    assert connection_proxy.closed is True
    assert read_version_metadata(database_path) == metadata_before


def test_update_version_close_and_reporting_failures_after_commit_keep_success(
    monkeypatch, local_tmp_path
):
    database_path = local_tmp_path / "system_data.db"
    create_version_db(database_path, "0.26.02.01")
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.06.01")
    monkeypatch.setattr(
        upgrade_db.LogManager,
        "set_log_handler",
        staticmethod(lambda _name: RaisingInfoLogger()),
    )
    manager = DataBaseManager(str(database_path))
    connection_proxy = ConnectionProxy(manager.conn, fail_close=True)
    manager.conn = connection_proxy

    with monkeypatch.context() as reporting_context:
        reporting_context.setattr(
            "builtins.print",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("console failed after commit")
            ),
        )
        result = manager.update_version()

    assert result is True
    assert connection_proxy.closed is True
    assert read_version_value(database_path, "last_running_version") == "0.26.02.01"
    assert read_version_value(database_path, "current_version") == "0.26.06.01"
    assert read_version_value(database_path, "update_time") != "2000-01-01 00:00:00"


@pytest.mark.parametrize("reporting_failure", ["logger", "print"])
def test_update_version_reporting_failure_after_commit_is_non_fatal(
    monkeypatch, local_tmp_path, reporting_failure
):
    database_path = local_tmp_path / "system_data.db"
    create_version_db(database_path, "0.26.02.01")
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.06.01")
    logger = RaisingInfoLogger() if reporting_failure == "logger" else DummyLogger()
    monkeypatch.setattr(upgrade_db.LogManager, "set_log_handler", staticmethod(lambda _name: logger))
    manager = DataBaseManager(str(database_path))

    if reporting_failure == "print":
        with monkeypatch.context() as reporting_context:
            reporting_context.setattr(
                "builtins.print",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("print failed after commit")),
            )
            result = manager.update_version()
    else:
        result = manager.update_version()

    assert result is True
    assert read_version_value(database_path, "last_running_version") == "0.26.02.01"
    assert read_version_value(database_path, "current_version") == "0.26.06.01"
    assert read_version_value(database_path, "update_time") != "2000-01-01 00:00:00"
