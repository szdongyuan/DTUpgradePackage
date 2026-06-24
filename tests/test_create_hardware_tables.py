import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from create_hardware_tables import create_hardware_tables, main


VALID_SELECTED_DEVICES = {
    "mic": {
        "name": "麦克风 (6- High Definition Audio D",
        "hostapi_name": "MME",
        "default_samplerate": 44100.0,
    },
    "speaker": {
        "name": "扬声器 (6- High Definition Audio D",
        "hostapi_name": "MME",
        "default_samplerate": 44100.0,
    },
    "mic_channels": [0],
}

VALID_CALIBRATION = {
    "version": 1,
    "channels": {
        "0": {
            "v2pa_factor": 1.0916916788093647,
            "calibrated_at": "2026-05-29",
            "standard_spl": 94,
        }
    },
}


def _create_empty_system_db(tmp_path):
    db_path = tmp_path / "database" / "system_data.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.close()
    return db_path


def _write_json(tmp_path, relative_path, data):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _write_text(tmp_path, relative_path, text):
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(db_path):
    with _connect(db_path) as conn:
        return {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def _fetch_all(db_path, sql, params=()):
    with _connect(db_path) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _fetch_one(db_path, sql, params=()):
    rows = _fetch_all(db_path, sql, params)
    assert len(rows) == 1
    return rows[0]


def _asset_rows(db_path):
    return _fetch_all(
        db_path,
        "SELECT * FROM hardware_assets ORDER BY hardware_type",
    )


def _channel_rows(db_path):
    return _fetch_all(
        db_path,
        "SELECT * FROM hardware_channel_calibrations ORDER BY direction, channel_index",
    )


def test_missing_legacy_json_files_create_tables_and_warn(tmp_path, monkeypatch):
    db_path = _create_empty_system_db(tmp_path)
    monkeypatch.chdir(tmp_path)

    warnings = create_hardware_tables()

    assert {"hardware_assets", "hardware_channel_calibrations"}.issubset(
        _table_names(db_path)
    )
    assert _fetch_all(db_path, "SELECT * FROM hardware_assets") == []
    assert any("警告" in warning for warning in warnings)
    assert any("last_audio_devices.json" in warning for warning in warnings)


def test_valid_legacy_hardware_import_creates_assets_and_channels(tmp_path, monkeypatch):
    db_path = _create_empty_system_db(tmp_path)
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", VALID_SELECTED_DEVICES)
    monkeypatch.chdir(tmp_path)

    warnings = create_hardware_tables()

    mic = _fetch_one(
        db_path,
        "SELECT * FROM hardware_assets WHERE hardware_type = 'microphone'",
    )
    assert mic["samplerate"] == 44100
    assert mic["bit_depth"] == 32
    assert mic["latency_ms"] == 100
    assert mic["max_input_channels"] == 1
    assert mic["max_output_channels"] == 0

    speaker = _fetch_one(
        db_path,
        "SELECT * FROM hardware_assets WHERE hardware_type = 'speaker'",
    )
    assert speaker["samplerate"] == 44100
    assert speaker["bit_depth"] == 32
    assert speaker["latency_ms"] == 100
    assert speaker["max_input_channels"] == 0
    assert speaker["max_output_channels"] == 1

    assert len(_asset_rows(db_path)) == 2
    assert {
        (row["direction"], row["channel_index"], row["channel_label"])
        for row in _channel_rows(db_path)
    } == {("input", 0, "In1"), ("output", 0, "Out1")}
    assert any("硬件注册信息不完整" in warning for warning in warnings)


def test_import_rerun_is_deterministic_and_does_not_duplicate_rows(tmp_path, monkeypatch):
    db_path = _create_empty_system_db(tmp_path)
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", VALID_SELECTED_DEVICES)
    monkeypatch.chdir(tmp_path)

    create_hardware_tables()
    first_asset_ids = {
        row["hardware_id"]
        for row in _fetch_all(db_path, "SELECT hardware_id FROM hardware_assets")
    }
    first_channel_ids = {
        row["channel_id"]
        for row in _fetch_all(db_path, "SELECT channel_id FROM hardware_channel_calibrations")
    }

    create_hardware_tables()

    assert _fetch_one(db_path, "SELECT COUNT(*) AS count FROM hardware_assets")["count"] == 2
    assert (
        _fetch_one(db_path, "SELECT COUNT(*) AS count FROM hardware_channel_calibrations")[
            "count"
        ]
        == 2
    )
    for row_id in first_asset_ids | first_channel_ids:
        assert str(uuid.UUID(row_id)) == row_id
    assert first_asset_ids == {
        row["hardware_id"]
        for row in _fetch_all(db_path, "SELECT hardware_id FROM hardware_assets")
    }
    assert first_channel_ids == {
        row["channel_id"]
        for row in _fetch_all(db_path, "SELECT channel_id FROM hardware_channel_calibrations")
    }


def test_calibration_sync_imports_and_preserves_existing_payload(tmp_path, monkeypatch):
    db_path = _create_empty_system_db(tmp_path)
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", VALID_SELECTED_DEVICES)
    calibration_path = _write_json(
        tmp_path,
        "ui/ui_config/mic_channel_calibration.json",
        VALID_CALIBRATION,
    )
    monkeypatch.chdir(tmp_path)

    create_hardware_tables()

    input_channel = _fetch_one(
        db_path,
        "SELECT * FROM hardware_channel_calibrations WHERE direction = 'input'",
    )
    assert input_channel["calibration_type"] == "v2pa"
    assert input_channel["factor_value"] == 1.0916916788093647
    assert input_channel["standard_spl"] == 94
    assert input_channel["max_voltage"] is None
    assert "calibrated_at" in input_channel["coefficients_json"]

    calibration_path.unlink()
    create_hardware_tables()

    preserved_channel = _fetch_one(
        db_path,
        "SELECT * FROM hardware_channel_calibrations WHERE direction = 'input'",
    )
    assert preserved_channel["calibration_type"] == "v2pa"
    assert preserved_channel["factor_value"] == 1.0916916788093647
    assert preserved_channel["standard_spl"] == 94
    assert "calibrated_at" in preserved_channel["coefficients_json"]


def test_valid_selected_devices_without_calibration_imports_placeholders(tmp_path, monkeypatch):
    db_path = _create_empty_system_db(tmp_path)
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", VALID_SELECTED_DEVICES)
    monkeypatch.chdir(tmp_path)

    warnings = create_hardware_tables()

    assert len(_asset_rows(db_path)) == 2
    assert len(_channel_rows(db_path)) == 2
    input_channel = _fetch_one(
        db_path,
        "SELECT * FROM hardware_channel_calibrations WHERE direction = 'input'",
    )
    assert input_channel["calibration_type"] is None
    assert input_channel["factor_value"] is None
    assert input_channel["standard_spl"] is None
    assert input_channel["max_voltage"] is None
    assert input_channel["coefficients_json"] is None
    assert any("校准" in warning and "缺失" in warning for warning in warnings)


def test_misspelled_calibration_fallback_imports_channel_zero_only(tmp_path, monkeypatch):
    db_path = _create_empty_system_db(tmp_path)
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", VALID_SELECTED_DEVICES)
    _write_json(
        tmp_path,
        "ui/ui_config/mic_channel_calibration.josn",
        {
            "version": 1,
            "channels": {
                "0": {"v2pa_factor": 2.5, "standard_spl": 94},
                "1": {"v2pa_factor": 3.5, "standard_spl": 94},
            },
        },
    )
    monkeypatch.chdir(tmp_path)

    warnings = create_hardware_tables()

    input_channel = _fetch_one(
        db_path,
        "SELECT * FROM hardware_channel_calibrations WHERE direction = 'input'",
    )
    assert input_channel["factor_value"] == 2.5
    assert (
        _fetch_one(
            db_path,
            "SELECT COUNT(*) AS count FROM hardware_channel_calibrations "
            "WHERE direction = 'input' AND channel_index = 1",
        )["count"]
        == 0
    )
    assert any("忽略" in warning and "通道" in warning for warning in warnings)


@pytest.mark.parametrize(
    "calibration",
    [
        {"version": 1},
        {"version": 1, "channels": []},
        {"version": 1, "channels": {"1": {"v2pa_factor": 1.0, "standard_spl": 94}}},
        {"version": 1, "channels": {"0": "bad"}},
        {"version": 1, "channels": {"0": {"standard_spl": 94}}},
        {"version": 1, "channels": {"0": {"v2pa_factor": "bad", "standard_spl": 94}}},
        {"version": 1, "channels": {"0": {"v2pa_factor": 1.0}}},
        {"version": 1, "channels": {"0": {"v2pa_factor": 1.0, "standard_spl": "bad"}}},
    ],
)
def test_invalid_calibration_content_warns_without_failing(tmp_path, monkeypatch, calibration):
    db_path = _create_empty_system_db(tmp_path)
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", VALID_SELECTED_DEVICES)
    _write_json(tmp_path, "ui/ui_config/mic_channel_calibration.json", calibration)
    monkeypatch.chdir(tmp_path)

    warnings = create_hardware_tables()

    assert len(_asset_rows(db_path)) == 2
    input_channel = _fetch_one(
        db_path,
        "SELECT * FROM hardware_channel_calibrations WHERE direction = 'input'",
    )
    assert input_channel["calibration_type"] is None
    assert input_channel["factor_value"] is None
    assert input_channel["standard_spl"] is None
    assert any("警告" in warning for warning in warnings)


@pytest.mark.parametrize(
    ("selected_devices", "expected_types", "warning_text"),
    [
        (
            {**VALID_SELECTED_DEVICES, "mic": None},
            {"speaker"},
            "麦克风",
        ),
        (
            {**VALID_SELECTED_DEVICES, "speaker": None},
            {"microphone"},
            "扬声器",
        ),
        (
            {
                **VALID_SELECTED_DEVICES,
                "mic": {"hostapi_name": "MME", "default_samplerate": 44100.0},
            },
            {"speaker"},
            "麦克风",
        ),
        (
            {
                **VALID_SELECTED_DEVICES,
                "speaker": {"name": "扬声器", "default_samplerate": 44100.0},
            },
            {"microphone"},
            "扬声器",
        ),
    ],
)
def test_incomplete_selected_device_side_skips_only_that_side(
    tmp_path,
    monkeypatch,
    selected_devices,
    expected_types,
    warning_text,
):
    db_path = _create_empty_system_db(tmp_path)
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", selected_devices)
    monkeypatch.chdir(tmp_path)

    warnings = create_hardware_tables()

    assert {row["hardware_type"] for row in _asset_rows(db_path)} == expected_types
    assert any(warning_text in warning for warning in warnings)
    if expected_types == {"microphone"}:
        assert any("导入麦克风" in warning and "扬声器" not in warning for warning in warnings)
    if expected_types == {"speaker"}:
        assert any("导入扬声器" in warning and "麦克风输入" not in warning for warning in warnings)


@pytest.mark.parametrize("samplerate", [None, 96000, "bad"])
def test_missing_or_unsupported_samplerate_falls_back_to_48000(
    tmp_path,
    monkeypatch,
    samplerate,
):
    db_path = _create_empty_system_db(tmp_path)
    selected_devices = json.loads(json.dumps(VALID_SELECTED_DEVICES, ensure_ascii=False))
    if samplerate is None:
        selected_devices["mic"].pop("default_samplerate")
    else:
        selected_devices["mic"]["default_samplerate"] = samplerate
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", selected_devices)
    monkeypatch.chdir(tmp_path)

    warnings = create_hardware_tables()

    mic = _fetch_one(
        db_path,
        "SELECT * FROM hardware_assets WHERE hardware_type = 'microphone'",
    )
    assert mic["samplerate"] == 48000
    assert any("采样率" in warning for warning in warnings)


@pytest.mark.parametrize(
    "relative_path",
    [
        "ui/ui_config/last_audio_devices.json",
        "ui/ui_config/mic_channel_calibration.json",
        "ui/ui_config/mic_channel_calibration.josn",
    ],
)
def test_malformed_json_rolls_back_and_main_returns_failure(
    tmp_path,
    monkeypatch,
    capsys,
    relative_path,
):
    db_path = _create_empty_system_db(tmp_path)
    if "last_audio_devices" not in relative_path:
        _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", VALID_SELECTED_DEVICES)
    _write_text(tmp_path, relative_path, "{bad json")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(json.JSONDecodeError):
        create_hardware_tables()

    assert "hardware_assets" not in _table_names(db_path)
    assert "hardware_channel_calibrations" not in _table_names(db_path)

    assert main() == 1
    assert "失败" in capsys.readouterr().out
