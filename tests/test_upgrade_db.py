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


@pytest.fixture
def local_tmp_path():
    path = Path(__file__).parent / ".tmp" / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def create_version_db(path, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE system_info_table (name TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO system_info_table (name, value) VALUES ('current_version', ?)", (version,))
        conn.execute("INSERT INTO system_info_table (name, value) VALUES ('last_running_version', null)")
        conn.execute("INSERT INTO system_info_table (name, value) VALUES ('update_time', null)")
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


def test_update_version_writes_to_system_database_after_split(monkeypatch, local_tmp_path):
    system_path = local_tmp_path / "system_data.db"
    create_version_db(system_path, "0.26.02.01")
    monkeypatch.setattr(upgrade_db, "TARGET_VERSION", "0.26.06.01")
    monkeypatch.setattr(upgrade_db.LogManager, "set_log_handler", staticmethod(lambda _name: DummyLogger()))

    DataBaseManager(str(system_path)).update_version()

    assert read_version_value(system_path, "last_running_version") == "0.26.02.01"
    assert read_version_value(system_path, "current_version") == "0.26.06.01"
