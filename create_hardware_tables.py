import json
import sqlite3
import sys
import uuid
from pathlib import Path


DEFAULT_DB_PATH = Path("database") / "system_data.db"
DEFAULT_UI_CONFIG_DIR = Path("ui") / "ui_config"
DEFAULT_SELECTED_DEVICES_PATH = DEFAULT_UI_CONFIG_DIR / "last_audio_devices.json"
DEFAULT_CALIBRATION_PATH = DEFAULT_UI_CONFIG_DIR / "mic_channel_calibration.json"
FALLBACK_CALIBRATION_PATH = DEFAULT_UI_CONFIG_DIR / "mic_channel_calibration.josn"
LEGACY_IMPORT_UUID_NAMESPACE = uuid.UUID("7d65144d-3935-5ca8-9f30-3f6ea9e62324")

WARNING_BOTH_IMPORTED = (
    "警告：已根据旧配置文件导入麦克风和扬声器，但硬件注册信息不完整，"
    "麦克风输入通道数按 1 处理，扬声器输出通道数按 1 处理，"
    "建议在硬件管理中重新注册麦克风和扬声器。"
)
WARNING_MIC_IMPORTED = (
    "警告：已根据旧配置文件导入麦克风，但硬件注册信息不完整，"
    "麦克风输入通道数按 1 处理，建议在硬件管理中重新注册麦克风。"
)
WARNING_SPEAKER_IMPORTED = (
    "警告：已根据旧配置文件导入扬声器，但硬件注册信息不完整，"
    "扬声器输出通道数按 1 处理，建议在硬件管理中重新注册扬声器。"
)
WARNING_SELECTED_MISSING = (
    "警告：旧硬件选择文件 last_audio_devices.json 缺失，已创建硬件表但跳过旧硬件导入。"
)
WARNING_CALIBRATION_MISSING = (
    "警告：旧麦克风校准 JSON 缺失，已导入硬件和通道占位信息但跳过校准同步。"
)
WARNING_MIC_SKIPPED = "警告：旧配置中的麦克风信息缺失或不完整，已跳过麦克风导入。"
WARNING_SPEAKER_SKIPPED = "警告：旧配置中的扬声器信息缺失或不完整，已跳过扬声器导入。"
WARNING_SAMPLERATE_FALLBACK = "警告：旧配置中的采样率缺失或不支持，已按 48000 处理。"
WARNING_CALIBRATION_INVALID = "警告：旧麦克风校准内容缺失或不完整，已跳过校准同步。"
WARNING_EXTRA_CALIBRATION_CHANNELS = (
    "警告：旧麦克风校准包含非 0 通道；本次旧配置导入只注册 1 个输入通道，已忽略其他通道。"
)


CREATE_HARDWARE_ASSETS_SQL = """
CREATE TABLE IF NOT EXISTS hardware_assets(
    hardware_id TEXT PRIMARY KEY,
    hardware_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    device_name TEXT NOT NULL,
    hostapi_name TEXT NOT NULL,
    samplerate INTEGER NOT NULL,
    bit_depth INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    max_input_channels INTEGER NOT NULL CHECK (max_input_channels >= 0),
    max_output_channels INTEGER NOT NULL CHECK (max_output_channels >= 0),
    updated_at TEXT DEFAULT (DATETIME('now', '+8 hours'))
)
"""

CREATE_CHANNEL_CALIBRATIONS_SQL = """
CREATE TABLE IF NOT EXISTS hardware_channel_calibrations(
    channel_id TEXT PRIMARY KEY,
    hardware_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    channel_index INTEGER NOT NULL,
    channel_label TEXT NOT NULL,
    calibration_type TEXT,
    factor_value REAL,
    standard_spl REAL,
    max_voltage REAL,
    coefficients_json TEXT,
    updated_at TEXT DEFAULT (DATETIME('now', '+8 hours')),
    FOREIGN KEY (hardware_id) REFERENCES hardware_assets (hardware_id)
        ON DELETE CASCADE ON UPDATE NO ACTION
)
"""


def _legacy_uuid(name: str) -> str:
    return str(uuid.uuid5(LEGACY_IMPORT_UUID_NAMESPACE, name))


def _load_json_if_exists(path: Path):
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8-sig") as file:
        return json.load(file)


def _select_calibration_path(config_dir: Path) -> Path | None:
    calibration_path = config_dir / DEFAULT_CALIBRATION_PATH.name
    fallback_path = config_dir / FALLBACK_CALIBRATION_PATH.name
    if calibration_path.exists():
        return calibration_path
    if fallback_path.exists():
        return fallback_path
    return None


def _normalize_samplerate(value, warnings) -> int:
    try:
        samplerate = int(value)
    except (TypeError, ValueError):
        warnings.append(WARNING_SAMPLERATE_FALLBACK)
        return 48000

    if samplerate not in {44100, 48000}:
        warnings.append(WARNING_SAMPLERATE_FALLBACK)
        return 48000
    return samplerate


def _build_asset_payload(kind, data, warnings=None) -> dict | None:
    warnings = warnings if warnings is not None else []
    if not isinstance(data, dict) or not data.get("name") or not data.get("hostapi_name"):
        return None

    if kind == "mic":
        hardware_type = "microphone"
        max_input_channels = 1
        max_output_channels = 0
    elif kind == "speaker":
        hardware_type = "speaker"
        max_input_channels = 0
        max_output_channels = 1
    else:
        raise ValueError(f"Unsupported legacy hardware kind: {kind}")

    name = str(data["name"])
    hostapi_name = str(data["hostapi_name"])
    hardware_id = _legacy_uuid(f"legacy-hardware-json:{kind}:{hostapi_name}:{name}")

    return {
        "hardware_id": hardware_id,
        "hardware_type": hardware_type,
        "display_name": name,
        "device_name": name,
        "hostapi_name": hostapi_name,
        "samplerate": _normalize_samplerate(data.get("default_samplerate"), warnings),
        "bit_depth": 32,
        "latency_ms": 100,
        "max_input_channels": max_input_channels,
        "max_output_channels": max_output_channels,
    }


def _build_channel_id(hardware_id, direction, channel_index) -> str:
    return _legacy_uuid(f"legacy-hardware-json:{hardware_id}:{direction}:{channel_index}")


def _create_tables(conn):
    conn.execute(CREATE_HARDWARE_ASSETS_SQL)
    conn.execute(CREATE_CHANNEL_CALIBRATIONS_SQL)


def _upsert_hardware_asset(conn, payload):
    conn.execute(
        """
        INSERT INTO hardware_assets (
            hardware_id,
            hardware_type,
            display_name,
            device_name,
            hostapi_name,
            samplerate,
            bit_depth,
            latency_ms,
            max_input_channels,
            max_output_channels
        )
        VALUES (
            :hardware_id,
            :hardware_type,
            :display_name,
            :device_name,
            :hostapi_name,
            :samplerate,
            :bit_depth,
            :latency_ms,
            :max_input_channels,
            :max_output_channels
        )
        ON CONFLICT(hardware_id) DO UPDATE SET
            hardware_type = excluded.hardware_type,
            display_name = excluded.display_name,
            device_name = excluded.device_name,
            hostapi_name = excluded.hostapi_name,
            samplerate = excluded.samplerate,
            bit_depth = excluded.bit_depth,
            latency_ms = excluded.latency_ms,
            max_input_channels = excluded.max_input_channels,
            max_output_channels = excluded.max_output_channels,
            updated_at = DATETIME('now', '+8 hours')
        """,
        payload,
    )


def _upsert_channel_placeholder(conn, hardware_id, direction, channel_index, channel_label):
    conn.execute(
        """
        INSERT INTO hardware_channel_calibrations (
            channel_id,
            hardware_id,
            direction,
            channel_index,
            channel_label
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET
            hardware_id = excluded.hardware_id,
            direction = excluded.direction,
            channel_index = excluded.channel_index,
            channel_label = excluded.channel_label,
            updated_at = DATETIME('now', '+8 hours')
        """,
        (
            _build_channel_id(hardware_id, direction, channel_index),
            hardware_id,
            direction,
            channel_index,
            channel_label,
        ),
    )


def _load_legacy_selected_devices(ui_config_dir: Path, warnings):
    selected_devices_path = ui_config_dir / DEFAULT_SELECTED_DEVICES_PATH.name
    selected_devices = _load_json_if_exists(selected_devices_path)
    if selected_devices is None:
        warnings.append(WARNING_SELECTED_MISSING)
        return None
    if not isinstance(selected_devices, dict):
        warnings.append(WARNING_MIC_SKIPPED)
        warnings.append(WARNING_SPEAKER_SKIPPED)
        return {}
    return selected_devices


def _append_imported_hardware_warning(imported):
    imported_types = {payload["hardware_type"] for payload in imported}
    if imported_types == {"microphone", "speaker"}:
        return WARNING_BOTH_IMPORTED
    if imported_types == {"microphone"}:
        return WARNING_MIC_IMPORTED
    if imported_types == {"speaker"}:
        return WARNING_SPEAKER_IMPORTED
    return None


def _import_legacy_hardware(conn, selected_devices, warnings):
    imported = []

    mic_payload = _build_asset_payload("mic", selected_devices.get("mic"), warnings)
    if mic_payload is None:
        warnings.append(WARNING_MIC_SKIPPED)
    else:
        _upsert_hardware_asset(conn, mic_payload)
        _upsert_channel_placeholder(conn, mic_payload["hardware_id"], "input", 0, "In1")
        imported.append(mic_payload)

    speaker_payload = _build_asset_payload("speaker", selected_devices.get("speaker"), warnings)
    if speaker_payload is None:
        warnings.append(WARNING_SPEAKER_SKIPPED)
    else:
        _upsert_hardware_asset(conn, speaker_payload)
        _upsert_channel_placeholder(conn, speaker_payload["hardware_id"], "output", 0, "Out1")
        imported.append(speaker_payload)

    imported_warning = _append_imported_hardware_warning(imported)
    if imported_warning:
        warnings.append(imported_warning)
    return mic_payload, speaker_payload


def _valid_numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _load_calibration(ui_config_dir: Path, warnings, warn_if_missing=True):
    calibration_path = _select_calibration_path(ui_config_dir)
    if calibration_path is None:
        if warn_if_missing:
            warnings.append(WARNING_CALIBRATION_MISSING)
        return None
    return _load_json_if_exists(calibration_path)


def _sync_calibration(conn, mic_payload, calibration, warnings):
    if mic_payload is None:
        return

    if not isinstance(calibration, dict):
        warnings.append(WARNING_CALIBRATION_INVALID)
        return

    channels = calibration.get("channels")
    if not isinstance(channels, dict):
        warnings.append(WARNING_CALIBRATION_INVALID)
        return

    extra_channels = [channel for channel in channels if str(channel) != "0"]
    if extra_channels:
        warnings.append(WARNING_EXTRA_CALIBRATION_CHANNELS)

    channel_zero = channels.get("0")
    if not isinstance(channel_zero, dict):
        warnings.append(WARNING_CALIBRATION_INVALID)
        return

    factor_value = channel_zero.get("v2pa_factor")
    standard_spl = channel_zero.get("standard_spl")
    if not _valid_numeric(factor_value) or not _valid_numeric(standard_spl):
        warnings.append(WARNING_CALIBRATION_INVALID)
        return

    conn.execute(
        """
        UPDATE hardware_channel_calibrations
        SET
            calibration_type = 'v2pa',
            factor_value = ?,
            standard_spl = ?,
            max_voltage = NULL,
            coefficients_json = ?,
            updated_at = DATETIME('now', '+8 hours')
        WHERE channel_id = ?
        """,
        (
            float(factor_value),
            standard_spl,
            json.dumps(channel_zero, ensure_ascii=False, sort_keys=True),
            _build_channel_id(mic_payload["hardware_id"], "input", 0),
        ),
    )


def create_hardware_tables(db_path=None, ui_config_dir=None):
    warnings = []
    database_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    config_dir = Path(ui_config_dir) if ui_config_dir is not None else DEFAULT_UI_CONFIG_DIR

    if not database_path.exists():
        raise FileNotFoundError(f"数据库文件不存在: {database_path}")

    conn = sqlite3.connect(database_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        _create_tables(conn)

        selected_devices = _load_legacy_selected_devices(config_dir, warnings)
        calibration = _load_calibration(
            config_dir,
            warnings,
            warn_if_missing=selected_devices is not None,
        )
        mic_payload = None
        if selected_devices is not None:
            mic_payload, _speaker_payload = _import_legacy_hardware(
                conn,
                selected_devices,
                warnings,
            )
            if calibration is not None:
                _sync_calibration(conn, mic_payload, calibration, warnings)

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return warnings


def main() -> int:
    try:
        warnings = create_hardware_tables()
    except Exception as exc:
        print(f"失败：创建硬件表或导入旧硬件 JSON 失败：{exc}")
        return 1

    print("硬件表已准备就绪。")
    for warning in warnings:
        print(warning)
    return 0


if __name__ == "__main__":
    sys.exit(main())
