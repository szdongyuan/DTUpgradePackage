# Hardware Tables Creation Script Design

## Purpose

Provide an independent Python program that adds the hardware asset and hardware channel calibration tables to the SQLite database at `./database/system_data.db`.

The program is intended for direct, local execution from the repository or packaged application working directory. It does not participate in the existing versioned database upgrade flow.

## Scope

In scope:

- Add a standalone script, `create_hardware_tables.py`.
- Connect to `database/system_data.db` relative to the current working directory by default.
- Require the database file to already exist.
- Enable SQLite foreign key enforcement for the connection.
- Create these tables with `CREATE TABLE IF NOT EXISTS`:
  - `hardware_assets`
  - `hardware_channel_calibrations`
- Commit both table creations as one transaction.
- Roll back and return a non-zero exit code if any database operation fails.
- Print a concise success or failure message.

Out of scope:

- Creating `database/system_data.db` when it is missing.
- Inserting seed hardware data.
- Updating `system_info_table` version values.
- Modifying existing version YAML files.
- Modifying the existing split-database migration behavior.
- Rebuilding executable artifacts.

## Behavior

When run from the intended working directory:

```powershell
python create_hardware_tables.py
```

the program uses:

```text
database/system_data.db
```

If the database exists, the program opens it, enables `PRAGMA foreign_keys = ON`, executes the two table creation statements, commits the transaction, closes the connection, prints a success message, and exits with code `0`.

If the database path does not exist, the program prints an error message and exits with code `1` without creating a new empty SQLite database.

If SQLite raises an error while creating the tables, the program rolls back the transaction, closes the connection, prints the error, and exits with code `1`.

Repeated runs are safe because both table statements use `CREATE TABLE IF NOT EXISTS`.

## Table Definitions

`hardware_assets`:

```sql
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
```

`hardware_channel_calibrations`:

```sql
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
```

## Compatibility / Migration

Backward compatibility: required.

Protected surfaces:

- Existing SQLite database contents.
- Existing upgrade command behavior.
- Existing version YAML behavior.
- Existing database path constants and application imports.

Allowed breakage:

- None for existing data or existing upgrade flows.
- The new script may fail fast when `database/system_data.db` is missing.

Migration strategy: standalone migration script.

The script performs only additive schema changes. Existing tables, rows, version metadata, and upgrade files are not changed.

## Testing

Automated tests should cover:

- Creating both tables in an existing temporary `database/system_data.db`.
- Re-running the script against the same database without error.
- Failing without creating a database when `database/system_data.db` is missing.
- Verifying the foreign key cascade behavior from `hardware_assets` to `hardware_channel_calibrations`.

Manual verification:

```powershell
python create_hardware_tables.py
```

Then inspect `database/system_data.db` with SQLite table metadata to confirm both tables exist.
