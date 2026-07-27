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


class ConnectionProxy:
    def __init__(self, connection, fail_select=False, fail_update=None, fail_commit=False):
        self.connection = connection
        self.fail_select = fail_select
        self.fail_update = fail_update
        self.fail_commit = fail_commit
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
        return self.connection.rollback()

    def close(self):
        self.closed = True
        return self.connection.close()


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
