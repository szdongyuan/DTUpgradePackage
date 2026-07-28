# Calibration Coefficients Import Design

## Context

`create_hardware_tables.py` creates the `hardware_assets` and
`hardware_channel_calibrations` tables, imports the legacy microphone and
speaker selected by `ui/ui_config/last_audio_devices.json`, and optionally
synchronizes microphone calibration data from
`ui/ui_config/mic_channel_calibration.json`.

Two corrections are required:

1. The microphone import currently writes `calibration_type = "v2pa"`. The
   application contract uses `mic_v2pa`.
2. The legacy speaker output-amplitude calibration stored in
   `ui/ui_config/calibration_coefficients.json` is not imported into the
   speaker output channel row.

The speaker calibration file is expected to exist in a normal installation
and has this shape:

```json
{
  "calibration_coefficients": [
    0.11150234741784035,
    1.2449542928034842e-17
  ],
  "max_voltage": 8.52
}
```

## Goals

- Write newly synchronized microphone calibration rows with
  `calibration_type = "mic_v2pa"`.
- Read the legacy speaker calibration file and synchronize its values to the
  imported speaker's output channel at index `0`.
- Match the database representation consumed by the target application.
- Preserve the existing deterministic IDs, rerun behavior, transaction
  boundary, and legacy-import warnings.

## Non-Goals

- Do not migrate arbitrary pre-existing database rows from `v2pa` to
  `mic_v2pa` when no microphone calibration source is synchronized.
- Do not change the database schema.
- Do not add support for more than the single legacy output channel registered
  by this importer.
- Do not change the format of the existing microphone calibration files.
- Do not copy `max_voltage` into `coefficients_json`.

## Design

### Configuration Path

Add a default path for:

`ui/ui_config/calibration_coefficients.json`

Path resolution must follow the existing `ui_config_dir` override used by
`create_hardware_tables()`, so tests and callers can supply an alternate
configuration directory.

### Microphone Calibration Type

Change the value written by the existing microphone synchronization update
from `v2pa` to `mic_v2pa`.

This change applies when a valid microphone calibration source is synchronized.
It is not a standalone migration of historical rows.

### Speaker Calibration Loading

Load `calibration_coefficients.json` during the existing table-creation and
legacy-import transaction.

The expected payload is an object containing:

- `calibration_coefficients`: a JSON array containing exactly two finite
  numeric values.
- `max_voltage`: a finite positive numeric value.

Booleans are not valid numeric values. Numeric integers and floats are
accepted and normalized to floats before storage.

Unknown top-level fields do not become part of the database payload.

### Speaker Channel Mapping

After the legacy speaker asset and its output channel placeholder have been
upserted, synchronize valid speaker calibration data to:

- the imported speaker's `hardware_id`
- `direction = "output"`
- `channel_index = 0`

Update the existing placeholder row; do not insert a second channel row.

The resulting columns must be:

| Column | Value |
| --- | --- |
| `calibration_type` | `"output_amplitude"` |
| `factor_value` | `NULL` |
| `standard_spl` | `NULL` |
| `max_voltage` | normalized `max_voltage` from the file |
| `coefficients_json` | `{"calibration_coefficients":[coefficient_0, coefficient_1]}` |
| `updated_at` | current application database time using the existing `+8 hours` convention |

`coefficients_json` must contain only the `calibration_coefficients` key because
the target application reads `max_voltage` from its dedicated database column
and validates that exact JSON key set.

If no valid speaker was imported, the calibration file cannot be associated
with hardware and no calibration row is written.

### Failure and Warning Behavior

- A syntactically malformed JSON file raises `json.JSONDecodeError`, rolls back
  the transaction, and causes the command entry point to report failure. This
  matches the existing malformed legacy JSON behavior.
- Although the file is expected to exist in production, an unexpectedly
  missing file does not prevent table creation or hardware import. The speaker
  placeholder remains unchanged and a Chinese warning is returned.
- A syntactically valid file with a non-object payload, missing required
  fields, invalid coefficient count, non-finite/non-numeric coefficients, or
  non-positive/non-finite/non-numeric `max_voltage` does not abort the
  transaction. The speaker placeholder remains unchanged and a Chinese warning
  is returned.
- If the file exists but no valid speaker is available, no speaker calibration
  warning is added beyond the existing warning explaining why the speaker was
  skipped.

### Rerun Semantics

The existing deterministic speaker and channel IDs remain unchanged.
Re-running with a valid file updates the same output row and does not create
duplicates.

If a row was synchronized successfully and a later run unexpectedly lacks the
speaker calibration file or contains semantically invalid data, the existing
calibration columns are preserved because the channel placeholder upsert does
not clear calibration data.

## Testing

Extend `tests/test_create_hardware_tables.py` with focused tests covering:

1. Existing microphone synchronization writes `mic_v2pa`.
2. A valid speaker calibration file writes `output_amplitude`, the dedicated
   `max_voltage` column, and a `coefficients_json` object containing only the
   normalized coefficient array.
3. Speaker calibration synchronization updates output channel `0` and does not
   create duplicate rows across reruns.
4. An unexpectedly missing speaker calibration file leaves the output
   placeholder uncalibrated and returns a warning.
5. Semantically invalid speaker calibration payloads leave the output
   placeholder uncalibrated and return a warning.
6. Malformed speaker calibration JSON rolls back table creation and makes
   `main()` return failure, consistent with the other legacy JSON files.
7. A previously synchronized output calibration remains intact when a later
   run lacks or cannot semantically use the speaker calibration file.

Run the focused test module and the complete test suite.

## Compatibility / Migration

- Backward compatibility: partial
- Protected surfaces:
  - existing `create_hardware_tables()` call signature
  - database schema
  - deterministic hardware and channel IDs
  - existing microphone calibration file formats
  - existing warning-and-continue behavior for missing or semantically invalid
    optional legacy data
  - transaction rollback behavior for malformed JSON
- Allowed breakage:
  - newly synchronized microphone rows use the corrected `mic_v2pa` value
    instead of the incorrect `v2pa` value
  - valid speaker calibration data now populates the existing output channel
    placeholder
- Migration strategy: no standalone migration; source-backed synchronization
  updates the existing deterministic rows when the corresponding valid source
  data is available
