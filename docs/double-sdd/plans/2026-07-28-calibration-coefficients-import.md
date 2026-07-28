# Calibration Coefficients Import Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct the microphone calibration type and import legacy speaker output-amplitude calibration into the existing output channel row.

**Architecture:** Extend the transactional legacy import in `create_hardware_tables.py`. Load the speaker JSON through the existing `ui_config_dir` override, safely validate and normalize its two coefficients plus maximum voltage, then update the deterministic speaker output-channel placeholder. Keep the microphone correction and speaker synchronization in one TDD slice because the same importer and test module own both.

**Tech Stack:** Python 3, SQLite, standard-library `json`/`math`/`pathlib`, pytest

---

## File Structure

- Modify `create_hardware_tables.py`
  - define the speaker calibration path and warnings
  - validate and synchronize speaker output calibration
  - correct the microphone calibration type
  - connect speaker synchronization to the existing transaction
- Modify `tests/test_create_hardware_tables.py`
  - update the microphone type contract
  - cover valid import, reruns, path overrides, warnings, preservation, validation,
    and malformed JSON

No new production module is needed because this behavior belongs to the
single-purpose legacy hardware importer and shares its transaction and channel
identity rules.

### Task 1: Import Microphone and Speaker Calibration Contracts

**Files:**
- Modify: `create_hardware_tables.py`
- Test: `tests/test_create_hardware_tables.py`

- [ ] **Step 0: Verify the isolated implementation baseline**

Work only in the dedicated worktree path supplied by the orchestrator. The
worktree must be created from the commit containing this reviewed plan; do not
copy uncommitted changes from the root working tree.

Run:

```powershell
git status --short
git diff --check
python -m pytest tests/test_create_hardware_tables.py -q
```

Expected:

- `git status --short` has no output
- `git diff --check` exits successfully
- the existing importer tests pass before any implementation edits

If the worktree is not clean or baseline collection fails, stop and report the
unexpected worktree state instead of editing around unrelated changes.

- [ ] **Step 1: Add the speaker fixture and update the microphone expectations**

Add near `VALID_CALIBRATION`:

```python
VALID_OUTPUT_CALIBRATION = {
    "calibration_coefficients": [
        0.11150234741784035,
        1.2449542928034842e-17,
    ],
    "max_voltage": 8.52,
}
```

In `test_calibration_sync_imports_and_preserves_existing_payload`, change both
expected microphone calibration types from `v2pa` to `mic_v2pa`.

- [ ] **Step 2: Write a failing valid-import and rerun test**

Add:

```python
def test_output_calibration_sync_imports_expected_database_shape_without_duplicates(
    tmp_path,
    monkeypatch,
):
    db_path = _create_empty_system_db(tmp_path)
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", VALID_SELECTED_DEVICES)
    _write_json(
        tmp_path,
        "ui/ui_config/calibration_coefficients.json",
        VALID_OUTPUT_CALIBRATION,
    )
    monkeypatch.chdir(tmp_path)

    create_hardware_tables()
    create_hardware_tables()

    output_channel = _fetch_one(
        db_path,
        "SELECT * FROM hardware_channel_calibrations WHERE direction = 'output'",
    )
    assert output_channel["channel_index"] == 0
    assert output_channel["channel_label"] == "Out1"
    assert output_channel["calibration_type"] == "output_amplitude"
    assert output_channel["factor_value"] is None
    assert output_channel["standard_spl"] is None
    assert output_channel["max_voltage"] == 8.52
    assert json.loads(output_channel["coefficients_json"]) == {
        "calibration_coefficients": [
            0.11150234741784035,
            1.2449542928034842e-17,
        ]
    }
    assert (
        _fetch_one(
            db_path,
            "SELECT COUNT(*) AS count FROM hardware_channel_calibrations "
            "WHERE direction = 'output'",
        )["count"]
        == 1
    )
```

- [ ] **Step 3: Write failing missing-file and semantic-validation tests**

Add:

```python
def test_missing_output_calibration_warns_and_leaves_placeholder(tmp_path, monkeypatch):
    db_path = _create_empty_system_db(tmp_path)
    _write_json(tmp_path, "ui/ui_config/last_audio_devices.json", VALID_SELECTED_DEVICES)
    monkeypatch.chdir(tmp_path)

    warnings = create_hardware_tables()

    output_channel = _fetch_one(
        db_path,
        "SELECT * FROM hardware_channel_calibrations WHERE direction = 'output'",
    )
    assert output_channel["calibration_type"] is None
    assert output_channel["max_voltage"] is None
    assert output_channel["coefficients_json"] is None
    assert any("扬声器输出幅度校准" in warning and "缺失" in warning for warning in warnings)
```

Add a parameterized
`test_invalid_output_calibration_warns_and_leaves_placeholder` covering:

```python
[
    None,
    [],
    {},
    {"max_voltage": 8.52},
    {"calibration_coefficients": [0.1], "max_voltage": 8.52},
    {"calibration_coefficients": [0.1, 0.2, 0.3], "max_voltage": 8.52},
    {"calibration_coefficients": [True, 0.2], "max_voltage": 8.52},
    {"calibration_coefficients": ["bad", 0.2], "max_voltage": 8.52},
    {"calibration_coefficients": [float("nan"), 0.2], "max_voltage": 8.52},
    {"calibration_coefficients": [float("inf"), 0.2], "max_voltage": 8.52},
    {"calibration_coefficients": [10**400, 0.2], "max_voltage": 8.52},
    {"calibration_coefficients": [0.1, 0.2]},
    {"calibration_coefficients": [0.1, 0.2], "max_voltage": True},
    {"calibration_coefficients": [0.1, 0.2], "max_voltage": "bad"},
    {"calibration_coefficients": [0.1, 0.2], "max_voltage": 0},
    {"calibration_coefficients": [0.1, 0.2], "max_voltage": -1},
    {"calibration_coefficients": [0.1, 0.2], "max_voltage": float("nan")},
    {"calibration_coefficients": [0.1, 0.2], "max_voltage": float("inf")},
    {"calibration_coefficients": [0.1, 0.2], "max_voltage": 10**400},
]
```

For each payload, write the selected devices and calibration JSON, run the
importer, assert the output placeholder calibration fields remain `NULL`, and
assert exactly one returned warning contains `扬声器输出幅度校准` and
`缺失或不完整`. Oversized integers must be treated as invalid data, not allowed
to raise `OverflowError`. The valid JSON `null` case must produce the semantic
invalid warning, not the missing-file warning.

- [ ] **Step 4: Write failing preservation, skipped-speaker, path-override, and malformed-file tests**

Add:

1. `test_output_calibration_is_preserved_when_later_source_is_missing`
   - run once with `VALID_OUTPUT_CALIBRATION`
   - remove `calibration_coefficients.json`
   - run again
   - assert `output_amplitude`, `max_voltage`, and decoded coefficients are
     unchanged
2. `test_output_calibration_is_preserved_when_later_source_is_invalid`
   - run once with `VALID_OUTPUT_CALIBRATION`
   - replace it with a semantically invalid payload
   - run again
   - assert the same three stored values are unchanged
3. `test_output_calibration_skips_warning_when_speaker_is_invalid`
   - write selected devices with `speaker=None`
   - write a valid output calibration file
   - assert the existing speaker-skip warning is present
   - assert no warning contains `扬声器输出幅度校准`
4. `test_output_calibration_respects_ui_config_dir_override`
   - create the database separately
   - write `last_audio_devices.json` and `calibration_coefficients.json` below
     an alternate configuration directory
   - call `create_hardware_tables(db_path=db_path, ui_config_dir=alternate_dir)`
   - assert the output row contains the expected calibration
5. Add `"ui/ui_config/calibration_coefficients.json"` to the parameter list of
   `test_malformed_json_rolls_back_and_main_returns_failure`.
6. `test_output_calibration_normalizes_integers_and_drops_unknown_fields`
   - write:

     ```python
     {
         "calibration_coefficients": [1, 2],
         "max_voltage": 9,
         "ignored": "must not be stored",
     }
     ```

   - assert the decoded database payload is exactly
     `{"calibration_coefficients": [1.0, 2.0]}`
   - assert the dedicated `max_voltage` column equals `9.0`

- [ ] **Step 5: Run the complete focused red suite**

Run:

```powershell
python -m pytest `
  tests/test_create_hardware_tables.py::test_calibration_sync_imports_and_preserves_existing_payload `
  tests/test_create_hardware_tables.py::test_output_calibration_sync_imports_expected_database_shape_without_duplicates `
  tests/test_create_hardware_tables.py::test_missing_output_calibration_warns_and_leaves_placeholder `
  tests/test_create_hardware_tables.py::test_invalid_output_calibration_warns_and_leaves_placeholder `
  tests/test_create_hardware_tables.py::test_output_calibration_is_preserved_when_later_source_is_missing `
  tests/test_create_hardware_tables.py::test_output_calibration_is_preserved_when_later_source_is_invalid `
  tests/test_create_hardware_tables.py::test_output_calibration_skips_warning_when_speaker_is_invalid `
  tests/test_create_hardware_tables.py::test_output_calibration_respects_ui_config_dir_override `
  tests/test_create_hardware_tables.py::test_output_calibration_normalizes_integers_and_drops_unknown_fields `
  tests/test_create_hardware_tables.py::test_malformed_json_rolls_back_and_main_returns_failure `
  -v
```

Expected: failures show the old `v2pa` value and absent speaker import,
validation, warning, and path-override behavior. Existing malformed cases may
pass, but the new speaker-file parameter must fail until its loader is added.

- [ ] **Step 6: Add the output path and warning constants**

Import `math` and define beside the existing calibration paths:

```python
import math

DEFAULT_OUTPUT_CALIBRATION_PATH = (
    DEFAULT_UI_CONFIG_DIR / "calibration_coefficients.json"
)
```

Define:

```python
WARNING_OUTPUT_CALIBRATION_MISSING = (
    "警告：旧扬声器输出幅度校准 JSON 缺失，已导入扬声器通道占位信息但跳过校准同步。"
)
WARNING_OUTPUT_CALIBRATION_INVALID = (
    "警告：旧扬声器输出幅度校准内容缺失或不完整，已跳过校准同步。"
)
```

- [ ] **Step 7: Implement loading and exception-safe numeric normalization**

Add:

```python
_MISSING_OUTPUT_CALIBRATION = object()


def _load_output_calibration(ui_config_dir: Path):
    output_calibration_path = ui_config_dir / DEFAULT_OUTPUT_CALIBRATION_PATH.name
    if not output_calibration_path.exists():
        return _MISSING_OUTPUT_CALIBRATION
    return _load_json_if_exists(output_calibration_path)
```

Add near `_valid_numeric`:

```python
def _normalize_finite_number(value):
    if not _valid_numeric(value):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(normalized):
        return None
    return normalized


def _normalize_output_calibration(calibration):
    if not isinstance(calibration, dict):
        return None

    coefficients = calibration.get("calibration_coefficients")
    if not isinstance(coefficients, (list, tuple)) or len(coefficients) != 2:
        return None

    normalized_coefficients = [
        _normalize_finite_number(coefficient) for coefficient in coefficients
    ]
    normalized_max_voltage = _normalize_finite_number(calibration.get("max_voltage"))
    if any(coefficient is None for coefficient in normalized_coefficients):
        return None
    if normalized_max_voltage is None or normalized_max_voltage <= 0:
        return None

    return normalized_coefficients, normalized_max_voltage
```

This must return `None`, not raise, for oversized integers and all other
semantically invalid numeric values.

- [ ] **Step 8: Implement the database synchronization and microphone correction**

Change the existing microphone SQL literal to:

```sql
calibration_type = 'mic_v2pa'
```

Add:

```python
def _sync_output_calibration(conn, speaker_payload, calibration, warnings):
    if speaker_payload is None:
        return
    if calibration is _MISSING_OUTPUT_CALIBRATION:
        warnings.append(WARNING_OUTPUT_CALIBRATION_MISSING)
        return

    normalized = _normalize_output_calibration(calibration)
    if normalized is None:
        warnings.append(WARNING_OUTPUT_CALIBRATION_INVALID)
        return

    coefficients, max_voltage = normalized
    conn.execute(
        """
        UPDATE hardware_channel_calibrations
        SET
            calibration_type = 'output_amplitude',
            factor_value = NULL,
            standard_spl = NULL,
            max_voltage = ?,
            coefficients_json = ?,
            updated_at = DATETIME('now', '+8 hours')
        WHERE channel_id = ?
        """,
        (
            max_voltage,
            json.dumps(
                {"calibration_coefficients": coefficients},
                ensure_ascii=False,
                sort_keys=True,
            ),
            _build_channel_id(speaker_payload["hardware_id"], "output", 0),
        ),
    )
```

Update only the deterministic output placeholder. Do not insert another row.

- [ ] **Step 9: Connect loading and synchronization to the transaction**

Inside the existing `try` block in `create_hardware_tables()`:

```python
output_calibration = _load_output_calibration(config_dir)
mic_payload = None
speaker_payload = None
if selected_devices is not None:
    mic_payload, speaker_payload = _import_legacy_hardware(
        conn,
        selected_devices,
        warnings,
    )
    if calibration is not None:
        _sync_calibration(conn, mic_payload, calibration, warnings)
    _sync_output_calibration(
        conn,
        speaker_payload,
        output_calibration,
        warnings,
    )
```

Requirements:

- load through the supplied `ui_config_dir`
- load inside the transaction so malformed JSON propagates and rolls back
- distinguish an absent file from a present file containing JSON `null`
- warn for a missing file only after a valid speaker exists
- produce no extra output-calibration warning for a skipped speaker
- preserve existing calibration when the source is later missing or invalid

- [ ] **Step 10: Run the focused green suite**

Run the exact command from Step 5 again.

Expected: every selected test and parameter case passes with zero failures.

- [ ] **Step 11: Run the full importer test module**

Run:

```powershell
python -m pytest tests/test_create_hardware_tables.py -v
```

Expected: all tests pass with zero failures.

- [ ] **Step 12: Run the complete test suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 13: Inspect and commit the implementation**

Run:

```powershell
git diff --check
git status --short
```

Confirm only the two planned files are modified, then:

```powershell
git add -- create_hardware_tables.py tests/test_create_hardware_tables.py
git commit -m "feat: import speaker calibration coefficients"
```

Record the base and head SHAs for paired spec and quality review.
