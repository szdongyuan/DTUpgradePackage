# YML JSON Merge Design

## Purpose

Add a native YML upgrade action that merges two JSON object files while
participating in the upgrade program's existing configuration backup and
rollback lifecycle.

The supplementary file contributes missing top-level fields to the target
file. The target file remains authoritative for fields present in both files.
The merged result overwrites the target file; the supplementary file is never
modified.

## Scope

In scope:

- Add a reusable two-file JSON merge helper in `base/json_merge.py`.
- Extend `ConfigManager.update_config_file` to process an optional
  `json_merge` list in each version manifest.
- Resolve merge targets relative to the target-program root.
- Resolve supplementary sources relative to the upgrade-package root.
- Back up the target's managed configuration directory before the first merge.
- Restore the pre-upgrade target configuration when this or any later upgrade
  step fails.
- Harden configuration backup/restore result handling so a failed backup cannot
  authorize a write and a failed restore cannot delete recovery data.
- Place final database-version persistence inside the same orchestration
  rollback boundary, make that persistence transactionally report success or
  failure, and retain all backups until it succeeds.
- Execute configured merges in version order.
- Add focused helper, configuration-manager, orchestration rollback, and YML
  tests.

Out of scope:

- Recursive or deep merging of nested objects.
- Merging arrays or JSON roots that are not objects.
- Merging more than two files in one action.
- Running the merge through `ExeUpgradeManager` or a separate executable.
- Modifying `base/load_config.py`, whose unrelated missing runtime constants
  currently prevent it from being a reliable dependency in this upgrade
  package.
- Adding a merge entry to `resources/version_0.26.07.01.yml` before the real
  target and supplementary filenames are supplied.
- Changing database, UI, or executable upgrade sections.

## Merge Helper

The new module-level function is:

```python
merge_json_files(first_json_path, second_json_path) -> bool
```

Parameters:

- `first_json_path`: target JSON path and authoritative source, supplied as a
  non-empty string or `os.PathLike`.
- `second_json_path`: supplementary JSON source path, supplied as a non-empty
  string or `os.PathLike`.

Return value:

- `True` when both files are valid JSON objects and the merged object is
  successfully written to the first file.
- `False` for invalid path arguments, missing or unreadable files, invalid
  JSON, non-object JSON roots, paths that identify the same underlying file, or
  write/replace failures.

Expected filesystem and JSON errors are converted to `False`.

The module also exposes an integration helper:

```python
merge_json_files_with_reason(first_json_path, second_json_path) -> tuple[bool, str]
```

The Boolean wrapper delegates to this detailed helper. The reason-code contract
is exhaustive:

- Success: `(True, "ok")`
- Invalid/empty/unsupported path argument: `(False, "invalid_path")`
- Missing path or path that is not a regular file: `(False, "missing_file")`
- Read/open/decode filesystem failure: `(False, "unreadable_file")`
- Both arguments identify the same underlying file: `(False, "same_file")`
- JSON syntax failure: `(False, "invalid_json")`
- Either JSON root is not an object: `(False, "non_object_root")`
- Temporary-file, write, flush, sync, or replacement failure:
  `(False, "write_failed")`

Reason codes never contain path values or JSON data. `ConfigManager` uses this
detailed result for logging without duplicating parsing or write logic.

## YML Schema

Each version manifest may contain an optional top-level `json_merge` list:

```yaml
json_merge:
    - target: "ui/ui_config/analysis_config.json"
      source: "resources/config/analysis_config_patch.json"
```

Each list item must be an object with exactly these required fields:

- `target`: non-empty relative path below one of the target program's managed
  configuration roots.
- `source`: non-empty relative path below the upgrade package
  `resources/config` directory.

Unknown fields are rejected so misspelled configuration does not silently pass.

Manifest behavior:

- A missing `json_merge` key or `json_merge: []` is a successful no-op.
- A non-list `json_merge` value fails configuration upgrade.
- An invalid entry fails configuration upgrade before that entry writes.
- Entries execute in listed order.
- Version manifests execute in `upgrade_version_list` order.

The existing `config` action for a version executes first. That version's
`json_merge` actions execute afterward. A manifest should not both replace and
merge the same target through `config`; this is rejected as ambiguous.

## Path Resolution

`target` is resolved against `DEFAULT_TARGET_DIR`.

Allowed target roots are the existing rollback-managed directories:

```text
ui/ui_config/
configs/aimodel_config/
configs/scanner_barcode_config/
```

The resolved target must remain inside one of those roots. Absolute paths, any
original `..` path component (including traversal that would normalize back
inside the root), and symbolic-link resolution outside the managed root are
rejected.

Both the lexical absolute target path and its resolved `realpath` must remain
inside the same allowed target root. A lexical path outside a root is rejected
even if a symlink resolves inward.

`source` is resolved against `DEFAULT_DIR` and must remain inside:

```text
resources/config/
```

Absolute paths, any original `..` path component (including traversal that
would normalize back inside `resources/config`), and symbolic-link resolution
outside this source root are rejected.

Both the lexical absolute source path and its resolved `realpath` must remain
inside `resources/config`.

Both resolved paths must already exist as readable files. The target and source
must not identify the same underlying file.

## Merge Behavior

The helper performs a shallow top-level merge:

```python
merged = dict(second_data)
merged.update(first_data)
```

This means:

- Keys found only in the target are preserved.
- Keys found only in the supplementary source are added.
- When both files contain the same top-level key, the complete target value is
  preserved.
- Nested dictionaries under a conflicting key are not recursively merged.
- Within one source object, duplicate JSON member names follow Python's
  standard `json.load` behavior: the last occurrence is retained before the
  two-file merge.
- Python's standard handling of `NaN`, `Infinity`, and `-Infinity` is retained.
- Top-level key order follows Python dictionary insertion behavior: source keys
  first, then target-only keys; replacing a duplicate does not move it.

Example:

```json
// target before merge
{"SPL": {"smooth_checked": true}, "AI": {"model": "target"}}

// supplementary source
{"Excel": {"enabled": true}, "AI": {"model": "source"}}

// target after merge
{
  "Excel": {"enabled": true},
  "AI": {"model": "target"},
  "SPL": {"smooth_checked": true}
}
```

## File Safety

Both files are decoded as UTF-8 JSON; a UTF-8 BOM is accepted. Both JSON roots
must be objects represented by Python dictionaries.

The target is replaced only after both files have been read and validated.
Writing uses a temporary file in the target directory, flushes and syncs it,
and atomically replaces the target with `os.replace`. Output is UTF-8 without a
BOM, uses `ensure_ascii=False`, and has an indentation level of six.

The supplementary source is opened read-only and remains byte-for-byte
unchanged.

Atomic replacement applies to the target directory entry. A symbolic-link
target entry is replaced without overwriting its former link target. Replacing
a hardlink target entry separates that path from other aliases, which retain
their prior contents. Path-containment checks may use the resolved real path,
but the lexical target path supplied by the caller must be retained for the
final `os.replace` operation so these semantics are preserved.

Rollback guarantees JSON content at the configured target path, not filesystem
link topology. If a target was a symbolic link or hardlink before the upgrade,
restoration may leave a regular file with the original JSON bytes at that path;
recreating the prior link relationship is out of scope.

## Backup And Rollback

Before the first merge into a managed configuration root, `ConfigManager`
invokes its existing backup behavior for that root. The whole managed
configuration directory is backed up once per upgrade run, matching ordinary
configuration upgrades.

The backup must succeed before a merge may write. `backup_config_file` must
check the Boolean result from `smart_copy`; it may change a root's backup flag
from `True` to `False` only after the copy returns `True`. A failed copy leaves
the flag unchanged, logs an error, and causes `update_config_file` to return
`False`.

The existing flags and backup paths are reused:

- `ui_config_flag` / `BACKUP_UI_CONFIG_PATH`
- `ai_model_config_flag` / `BACKUP_AI_MODEL_CONFIG_PATH`
- `scannre_barcode_config_flag` / `BACKUP_SCANNER_BARCODE_CONFIG_PATH`

On any merge validation or write failure, `update_config_file` returns `False`.
The existing `main.py` failure path calls `ConfigManager.restore_config`, which
restores every configuration root backed up during the run.

If a later database, executable, or program-update step fails, the same failure
path restores the original managed configuration directory, including the
pre-merge target JSON.

`restore_config` must check every `smart_copy` result and return a Boolean
overall result. It deletes configuration backups only when every required
restore succeeds. If any restore fails, it logs the failure, returns `False`,
and retains all configuration backups for manual recovery.

Multiple merges and multiple version manifests share the first backup of each
managed root. Rollback therefore restores the state from before the entire
upgrade run, not an intermediate merged state.

After a fully successful upgrade, the existing cleanup flow deletes the
configuration backup.

## Upgrade Orchestration Boundary

The current final database-version update must be moved inside the common
success/failure decision in `main.py`.

The testable orchestration API is:

```python
run_upgrade(logger=None) -> bool
```

It performs no pause and does not call `sys.exit`. It returns `True` after final
version persistence succeeds, including when only best-effort post-commit
cleanup fails. It returns `False` for initial backup failure or any pre-commit
upgrade/persistence failure after attempting the required restoration.

The `if __name__ == "__main__"` wrapper calls `run_upgrade`, preserves the
existing Windows pause behavior, and exits with code `0` for `True` or `1` for
`False`.

Required ordering:

1. Create database and configuration backups.
2. Run database, ordinary configuration, JSON merge, executable, and main
   program update steps.
3. Persist the final database version.
4. Only after every preceding operation succeeds, delete the database and
   configuration backups and exit successfully.

An exception or false result from final database-version persistence is an
upgrade failure. At that point the database backup still exists, so the common
failure path restores the database and calls `ConfigManager.restore_config`.

Configuration restoration is attempted even if database restoration fails.
Failures from either restore are logged and produce a failed process result;
recovery files that could not be applied are retained.

On the normal success path, orchestration must not remove
`audio_data_backup.db` or configuration backup directories before final
database-version persistence succeeds.

On a failed-upgrade path, successful restoration consumes the corresponding
backup and may delete it afterward. If any required restoration fails, recovery
backups are retained. This failure-path cleanup does not conflict with the
success-path rule above because final persistence will not be retried in that
run.

`DataBaseManager.update_version` may target either `audio_data.db` or
`system_data.db` through `get_version_database_path`. It must provide its own
transactional guarantee instead of relying on the legacy audio-only backup:

- A missing/unreadable current-version value is a transaction failure; metadata
  updates must not begin with a `None` previous version.
- The three version metadata updates execute in one SQLite transaction.
- The method returns `True` only after `commit()` succeeds.
- A read, SQL execution, or commit error triggers `rollback()` and returns
  `False`, leaving the selected version database's previous metadata intact.
- Connection cleanup always runs.
- Console or logger failures after a successful commit are non-fatal and cannot
  change the returned success result.

Because a reported failure cannot occur after a successful metadata commit,
the selected version database does not require a second file-level backup for
this final operation. `main.py` checks the Boolean return value and enters the
common failure path when it is `False`.

Database and configuration backup cleanup after a successful final commit is
best-effort housekeeping. A deletion failure is logged, all other cleanup
targets are still attempted, and `run_upgrade` continues to report success.
No pre-commit rollback is attempted after version metadata has committed.
Recovery files whose deletion failed remain available for manual cleanup.

## Logging

For each merge, `ConfigManager` prints and logs:

- The target and source relative paths when starting.
- A concise success message after the target is replaced.
- A concise failure reason category without including JSON contents.

Invalid YML, unsafe paths, missing files, invalid JSON, non-object roots, and
write failures all produce an error log and fail configuration upgrade.

## Compatibility / Migration

Backward compatibility: required for existing manifests.

Protected surfaces:

- Existing manifests without `json_merge`.
- Existing `config`, `db`, `ui`, and `exe` behavior.
- Successful-upgrade configuration backup cleanup behavior.
- Supplementary source file contents.
- Target JSON contents when validation, backup, or writing fails.

Allowed breakage:

- A manifest that defines an invalid `json_merge` section now fails the upgrade.
- Merge targets outside existing rollback-managed configuration roots are not
  supported.
- Existing backup/restore error handling is tightened: failed copies now fail
  the upgrade or restore instead of being logged as successful.
- `DataBaseManager.update_version` now returns a Boolean transaction result;
  existing callers that ignored its prior implicit `None` return are unaffected,
  while `main.py` consumes the result.

Migration strategy: additive YML schema extension.

No existing manifest requires migration. The previously discussed unused
`LoadUiConfig.append_sequence_config_registry_entry` method remains unchanged;
the upgrade path uses the independent merge helper to avoid coupling the
upgrade program to unrelated runtime configuration imports.

## Testing

Helper tests cover:

- Disjoint top-level fields from both files are present in the target.
- Duplicate top-level fields preserve the complete target value.
- Nested objects under a duplicate field are not recursively merged.
- The supplementary file remains byte-for-byte unchanged.
- Empty/unsupported paths, missing/unreadable files, invalid JSON, non-object
  roots, and same-file aliases return `False` without modifying either file.
- Every detailed-helper outcome returns the documented exhaustive reason code;
  reason strings contain no JSON or path data.
- UTF-8 BOM input and Python-supported non-finite numbers follow the documented
  behavior.
- Successful output is UTF-8 without BOM, preserves non-ASCII text, and uses
  six-space indentation.
- An in-root symbolic-link target is replaced at the lexical path while its
  former referent remains unchanged; a hardlink target separates from its alias,
  whose bytes remain unchanged.
- Simulated temporary creation, write, flush, sync, and replacement failures
  return `write_failed`, leave the target byte-for-byte unchanged, and clean up
  uncommitted temporary files.

Configuration-manager tests cover:

- Missing or empty `json_merge` is a successful no-op.
- Valid entries resolve paths against the documented roots and merge in order.
- Existing manifests behave unchanged.
- Invalid section/entry shapes and unknown fields fail.
- Absolute, traversal, and out-of-root paths fail.
- A target/source symbolic-link escape fails.
- A lexical path outside an allowed root fails even when its symlink resolves
  inward.
- A target also listed in `config` for the same version fails before merge.
- Backup occurs before the first write and only once per managed root.
- Backup failure prevents target modification and does not mark the root as
  backed up.
- Merge failure returns `False` and leaves restoration data available.
- Backup, restore, unsafe/missing path, merge start/success, merge reason-code,
  and orchestration restoration failures produce the documented logs without
  JSON contents.
- Restore checks copy results; failed restoration returns `False` and retains
  all backup data.
- Multiple versions and multiple merges roll back to the pre-upgrade state.

Orchestration tests cover:

- A simulated later executable or main-program update failure restores the
  target to its pre-upgrade bytes.
- A final database-version update `False` result enters the common rollback path
  and restores the target JSON.
- Database and configuration backups are not deleted before final
  database-version persistence succeeds.
- Configuration restoration is attempted when database restoration fails.
- A fully successful run performs final version persistence before deleting
  backups.

Database-version tests cover both selected database locations:

- Successful metadata persistence commits all three values and returns `True`.
- Missing/unreadable current-version data, SQL execution failure, and commit
  failure roll back all metadata changes and return `False`.
- A post-commit logging failure does not turn a committed update into a reported
  failure.
- Successful and failing cases exercise both legacy `audio_data.db` and split
  `system_data.db` version locations.

Post-commit cleanup tests cover:

- Database-backup deletion returning `False` or raising does not trigger
  rollback and does not prevent configuration-backup cleanup from being
  attempted.
- Configuration-backup deletion returning `False` or raising does not trigger
  rollback.
- The committed upgrade returns success while failed cleanup targets remain and
  warnings are logged.

The existing project test suite remains the regression check for unaffected
upgrade behavior.
