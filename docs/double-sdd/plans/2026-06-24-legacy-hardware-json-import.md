# Legacy Hardware JSON Import Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `create_hardware_tables.py` so it creates the hardware tables and imports legacy hardware/calibration JSON data into them.

**Architecture:** Keep this as a standalone Python utility at the repository root. The script will resolve database and JSON files relative to the process current working directory, use one SQLite transaction per run, keep missing legacy JSON non-fatal, and make malformed present JSON fatal. Tests exercise the helper functions and CLI behavior through temporary directories.

**Tech Stack:** Python standard library (`json`, `pathlib`, `sqlite3`, `sys`, `uuid`), pytest.

---

## File Structure

- Modify or create: `create_hardware_tables.py`
  - Owns table DDL, path constants, deterministic UUID generation, JSON loading, legacy hardware import, calibration sync, transaction handling, and CLI output.
  - Must remain executable as `python create_hardware_tables.py`.
- Modify or create: `tests/test_create_hardware_tables.py`
  - Covers existing table creation behavior and new legacy JSON import behavior.
- Do not modify:
  - `consts/program_config.py`
  - `control/upgrade_db.py`
  - `resources/version_*.yml`
  - executable artifacts under `dist/`
  - JSON source files under a real `ui/ui_config` directory, if present

## Task 1: Extend Standalone Script With Legacy JSON Import

**Files:**
- Modify/Create: `create_hardware_tables.py`
- Modify/Create: `tests/test_create_hardware_tables.py`

- [ ] **Step 1: Write tests for missing JSON behavior**

In `tests/test_create_hardware_tables.py`, keep or add helpers to create a temporary `database/system_data.db`, run the script helper from a temporary working directory, and query SQLite rows.

Add tests equivalent to:

```python
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
```

- [ ] **Step 2: Write tests for valid legacy hardware import**

Add a test that writes `ui/ui_config/last_audio_devices.json` with:

```json
{
  "mic": {
    "name": "麦克风 (6- High Definition Audio D",
    "hostapi_name": "MME",
    "default_samplerate": 44100.0
  },
  "speaker": {
    "name": "扬声器 (6- High Definition Audio D",
    "hostapi_name": "MME",
    "default_samplerate": 44100.0
  },
  "mic_channels": [0]
}
```

Assert:

- Two `hardware_assets` rows exist.
- Microphone row has `hardware_type = "microphone"`, `samplerate = 44100`, `bit_depth = 32`, `latency_ms = 100`, `max_input_channels = 1`, `max_output_channels = 0`.
- Speaker row has `hardware_type = "speaker"`, `samplerate = 44100`, `bit_depth = 32`, `latency_ms = 100`, `max_input_channels = 0`, `max_output_channels = 1`.
- Channel rows include microphone `direction = "input"`, `channel_index = 0`, `channel_label = "In1"`, and speaker `direction = "output"`, `channel_index = 0`, `channel_label = "Out1"`.
- The helper return value includes the required Chinese incomplete-registration warning.

- [ ] **Step 3: Write tests for deterministic rerun/upsert behavior**

Add a test that runs the import twice with the same `last_audio_devices.json`.

Assert:

- `hardware_assets` row count remains `2`.
- `hardware_channel_calibrations` row count remains `2`.
- IDs are valid UUID strings.
- The same IDs are present after both runs.

- [ ] **Step 4: Write tests for calibration sync and preservation**

Add a test that writes both:

- `ui/ui_config/last_audio_devices.json`
- `ui/ui_config/mic_channel_calibration.json`

Calibration JSON:

```json
{
  "version": 1,
  "channels": {
    "0": {
      "v2pa_factor": 1.0916916788093647,
      "calibrated_at": "2026-05-29",
      "standard_spl": 94
    }
  }
}
```

Assert the microphone input channel row has:

- `calibration_type = "v2pa"`
- `factor_value = 1.0916916788093647`
- `standard_spl = 94`
- `max_voltage IS NULL`
- `coefficients_json` contains `calibrated_at`.

Then delete the calibration JSON file, rerun import, and assert those calibration fields are preserved rather than cleared.

Also add a test for valid `last_audio_devices.json` with no calibration JSON file present.

Assert:

- Hardware assets and channel placeholders are imported.
- Calibration payload fields remain `NULL`.
- The helper return value includes a Chinese warning that calibration JSON is missing.
- The helper returns normally without raising.

- [ ] **Step 5: Write tests for `.josn` fallback and ignored extra channels**

Add a test where only `ui/ui_config/mic_channel_calibration.josn` exists and includes channels `0` and `1`.

Assert:

- Channel `0` calibration imports.
- No channel row for index `1` is created by calibration sync.
- The helper return value includes a Chinese warning that non-zero calibration channels were ignored.

- [ ] **Step 6: Write tests for invalid calibration content**

Add parametrized cases for present calibration JSON that should not fail:

- Missing `channels`.
- `channels` is not an object.
- Missing `channels["0"]`.
- `channels["0"]` is not an object.
- `v2pa_factor` missing or non-numeric.
- `standard_spl` missing or non-numeric.

Assert:

- The script exits through the helper without raising.
- Hardware rows still import when `last_audio_devices.json` is valid.
- Calibration payload fields remain `NULL`.
- The helper return value includes a Chinese warning.

- [ ] **Step 7: Write tests for incomplete selected-device JSON**

Add tests for these `last_audio_devices.json` cases:

- `mic` missing while `speaker` is valid.
- `speaker` missing while `mic` is valid.
- `mic` or `speaker` present but missing `name`.
- `mic` or `speaker` present but missing `hostapi_name`.
- `default_samplerate` missing or unsupported.

Assert:

- Missing or incomplete microphone data skips microphone import and returns a Chinese warning.
- Missing or incomplete speaker data skips speaker import and returns a Chinese warning.
- The valid side still imports when the other side is skipped.
- Missing or unsupported `default_samplerate` falls back to `48000` and returns a Chinese warning.
- The helper return value contains the matching Chinese incomplete-registration warning for only the imported side.

- [ ] **Step 8: Write tests for malformed JSON rollback**

Add tests for malformed present JSON files:

- `last_audio_devices.json` contains invalid JSON text.
- `mic_channel_calibration.json` contains invalid JSON text.
- `mic_channel_calibration.josn` contains invalid JSON text when the correctly spelled `.json` file is absent.

Assert:

- `create_hardware_tables()` raises `json.JSONDecodeError` or the public script-specific exception selected by implementation.
- Table creation/import transaction rolls back so neither `hardware_assets` nor `hardware_channel_calibrations` remains created in the empty test database.
- CLI `main()` returns `1` and prints a Chinese failure message for each malformed present JSON case.

- [ ] **Step 9: Run tests and confirm they fail before implementation**

Run:

```powershell
pytest tests/test_create_hardware_tables.py -v --basetemp .pytest_tmp
```

Expected: FAIL because JSON import behavior is not implemented yet.

- [ ] **Step 10: Implement constants and helpers**

In `create_hardware_tables.py`, add:

- `DEFAULT_UI_CONFIG_DIR = Path("ui") / "ui_config"`
- `DEFAULT_SELECTED_DEVICES_PATH = DEFAULT_UI_CONFIG_DIR / "last_audio_devices.json"`
- `DEFAULT_CALIBRATION_PATH = DEFAULT_UI_CONFIG_DIR / "mic_channel_calibration.json"`
- `FALLBACK_CALIBRATION_PATH = DEFAULT_UI_CONFIG_DIR / "mic_channel_calibration.josn"`
- `LEGACY_IMPORT_UUID_NAMESPACE = uuid.UUID("7d65144d-3935-5ca8-9f30-3f6ea9e62324")`
- Chinese warning message constants matching the spec.

Add helper functions:

- `_legacy_uuid(name: str) -> str`
- `_load_json_if_exists(path: Path)`
- `_select_calibration_path(config_dir: Path) -> Path | None`
- `_normalize_samplerate(value, warnings) -> int`
- `_build_asset_payload(kind, data) -> dict | None`
- `_build_channel_id(hardware_id, direction, channel_index) -> str`

- [ ] **Step 11: Implement table creation plus import transaction**

Update `create_hardware_tables(db_path=None, ui_config_dir=None)` so:

- `db_path` defaults to `database/system_data.db`.
- `ui_config_dir` defaults to `ui/ui_config`.
- Missing database remains `FileNotFoundError`.
- It opens SQLite, enables foreign keys, starts one transaction, creates both tables, reads/imports JSON, commits on success, rolls back on SQL or malformed JSON failures, and closes the connection.
- Malformed JSON is fatal for any present selected-device or calibration file:
  - `last_audio_devices.json`
  - `mic_channel_calibration.json`
  - `mic_channel_calibration.josn` when used as the fallback because `.json` is absent
- The helper returns `list[str]` warnings and does not print directly.
- Missing JSON files only append Chinese warnings to that return list and do not raise.

- [ ] **Step 12: Implement hardware asset upsert**

Use parameterized SQL with:

```sql
ON CONFLICT(hardware_id) DO UPDATE SET ...
```

Update exactly the fields listed in the spec and refresh `updated_at = DATETIME('now', '+8 hours')`.

Use deterministic UUIDv5 names:

- `legacy-hardware-json:mic:<hostapi_name>:<name>`
- `legacy-hardware-json:speaker:<hostapi_name>:<name>`

- [ ] **Step 13: Implement channel placeholder upsert**

Insert or update one microphone `In1` row and one speaker `Out1` row when their assets are imported.

Use:

- Microphone channel UUID name: `legacy-hardware-json:<mic hardware_id>:input:0`
- Speaker channel UUID name: `legacy-hardware-json:<speaker hardware_id>:output:0`

On conflict, update only `hardware_id`, `direction`, `channel_index`, `channel_label`, and `updated_at`. Do not clear calibration payload columns.

- [ ] **Step 14: Implement calibration sync**

When a valid calibration object exists at `channels["0"]`:

- Update the microphone input channel row with `calibration_type = "v2pa"`, `factor_value`, `standard_spl`, `max_voltage = NULL`, `coefficients_json = json.dumps(channel_obj, ensure_ascii=False, sort_keys=True)`, and refreshed `updated_at`.

For missing/invalid calibration content, append Chinese warnings to the helper return list and preserve existing calibration payload values.

For channels other than `"0"`, ignore them and append a Chinese warning to the helper return list.

- [ ] **Step 15: Implement CLI output**

Update `main()` so:

- Success prints table-ready status.
- It calls `warnings = create_hardware_tables()`.
- It prints each warning returned by the helper exactly once.
- Importing both microphone and speaker prints:

```text
警告：已根据旧配置文件导入麦克风和扬声器，但硬件注册信息不完整，麦克风输入通道数按 1 处理，扬声器输出通道数按 1 处理，建议在硬件管理中重新注册麦克风和扬声器。
```

- Importing only microphone prints:

```text
警告：已根据旧配置文件导入麦克风，但硬件注册信息不完整，麦克风输入通道数按 1 处理，建议在硬件管理中重新注册麦克风。
```

- Importing only speaker prints:

```text
警告：已根据旧配置文件导入扬声器，但硬件注册信息不完整，扬声器输出通道数按 1 处理，建议在硬件管理中重新注册扬声器。
```

- Fatal errors print a Chinese failure message and return `1`.

- [ ] **Step 16: Run focused tests**

Run:

```powershell
pytest tests/test_create_hardware_tables.py -v --basetemp .pytest_tmp
```

Expected: PASS.

- [ ] **Step 17: Run CLI manual verification**

Use a temp working directory:

```powershell
$repo = (Get-Location).Path
$tmp = Join-Path $repo ".manual-legacy-json-import"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force "$tmp\database" | Out-Null
New-Item -ItemType Directory -Force "$tmp\ui\ui_config" | Out-Null
python -c "import sqlite3; sqlite3.connect(r'$tmp\database\system_data.db').close()"
@'
{"mic":{"name":"麦克风 (6- High Definition Audio D","hostapi_name":"MME","default_samplerate":44100.0},"speaker":{"name":"扬声器 (6- High Definition Audio D","hostapi_name":"MME","default_samplerate":44100.0},"mic_channels":[0]}
'@ | Set-Content -Encoding UTF8 "$tmp\ui\ui_config\last_audio_devices.json"
@'
{"version":1,"channels":{"0":{"v2pa_factor":1.0916916788093647,"calibrated_at":"2026-05-29","standard_spl":94}}}
'@ | Set-Content -Encoding UTF8 "$tmp\ui\ui_config\mic_channel_calibration.json"
Push-Location $tmp
try {
    python "$repo\create_hardware_tables.py"
    python -c "import sqlite3; c=sqlite3.connect('database/system_data.db'); print(c.execute('SELECT hardware_type, display_name, max_input_channels, max_output_channels FROM hardware_assets ORDER BY hardware_type').fetchall()); print(c.execute('SELECT direction, channel_index, channel_label, calibration_type, factor_value, standard_spl FROM hardware_channel_calibrations ORDER BY direction').fetchall()); c.close()"
}
finally {
    Pop-Location
}
```

Expected:

- CLI exits `0`.
- Output includes the Chinese incomplete-registration warning.
- Query output includes microphone/speaker assets and input/output channel rows.
- Microphone channel row contains `v2pa` calibration values.

- [ ] **Step 18: Clean verification artifacts**

Remove only local verification artifacts created by this task:

```powershell
Remove-Item -Recurse -Force .pytest_tmp,.manual-legacy-json-import -ErrorAction SilentlyContinue
```

- [ ] **Step 19: Commit implementation**

Run:

```powershell
git add create_hardware_tables.py tests/test_create_hardware_tables.py
git commit -m "feat: import legacy hardware json"
```

Expected: commit includes only the script and its tests.
