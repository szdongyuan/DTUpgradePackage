# YML JSON Merge Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a rollback-aware native `json_merge` YML action that shallow-merges a supplementary JSON object into a target JSON object while preserving target values on top-level conflicts.

**Architecture:** Put filesystem-only merge behavior in a small `base/json_merge.py` module. Let `ConfigManager` own manifest validation, safe path resolution, managed-root backups, ordered execution, and restoration. Harden final version persistence into a Boolean SQLite transaction, then extract the `main.py` orchestration into a testable function so all pre-commit failures restore the merged configuration before backups are removed.

**Tech Stack:** Python standard library (`json`, `os`, `tempfile`, `sqlite3`, `shutil`), PyYAML through the existing loader, pytest.

---

## File Structure

- Create: `base/json_merge.py`
  - Owns two-file JSON validation, shallow target-priority merge, and atomic target replacement.
  - Has no dependency on `base/load_config.py` or runtime UI constants.
- Create: `tests/test_json_merge.py`
  - Covers helper behavior and file-safety failures.
- Modify: `control/upgrade_config.py`
  - Owns `json_merge` schema validation, safe path resolution, version ordering, backup-before-write, checked restoration, and backup retention.
- Create: `tests/test_upgrade_config.py`
  - Covers backup/restore state and native YML merge behavior.
- Modify: `control/upgrade_db.py`
  - Makes `DataBaseManager.update_version()` an explicit Boolean transaction.
- Modify: `tests/test_upgrade_db.py`
  - Covers successful commit, rollback on commit failure, and post-commit logging behavior.
- Modify: `main.py`
  - Extracts a testable orchestration function and keeps backup deletion after final version persistence.
- Create: `tests/test_main.py`
  - Covers success ordering and common rollback behavior.
- Do not modify:
  - `base/load_config.py`
  - `resources/version_0.26.07.01.yml` until real target/source filenames are supplied
  - existing executable artifacts under `dist/`
  - unrelated dirty-worktree files

## Task 1: Implement The Atomic Two-File Merge Helper

**Files:**
- Create: `base/json_merge.py`
- Create: `tests/test_json_merge.py`

- [ ] **Step 1: Write failing happy-path and conflict tests**

Create tests that write two temporary UTF-8 JSON objects, call
`merge_json_files(target, source)`, and assert:

```python
assert merge_json_files(target, source) is True
assert json.loads(target.read_text(encoding="utf-8")) == {
    "Excel": {"enabled": True},
    "AI": {"model": "target"},
    "SPL": {"smooth_checked": True},
}
assert source.read_bytes() == original_source_bytes
```

Include a nested collision proving the source's missing nested fields are not
recursively added when the target already contains that top-level key.

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_json_merge.py -q --basetemp=tests/.tmp/pytest-json-merge-red
```

Expected: collection/import failure because `base.json_merge` does not exist.

- [ ] **Step 3: Write failing validation and atomicity tests**

Add parameterized tests for:

- `None`, empty string, and unsupported path argument types.
- Missing target or source.
- Simulated `PermissionError` while opening either file.
- Invalid JSON in either file.
- List/scalar JSON roots.
- Same path and hardlink/symlink aliases where supported.
- UTF-8 BOM input.
- `NaN`, `Infinity`, and `-Infinity` using Python's standard JSON behavior.
- Successful output has no UTF-8 BOM, preserves literal non-ASCII text via
  `ensure_ascii=False`, and uses six-space indentation.
- Simulated failures from temporary-file creation, write, flush, `os.fsync`,
  and `os.replace`; each returns `(False, "write_failed")`, leaves both input
  files unchanged, and removes any uncommitted temporary file.

For every validation/read failure, explicitly assert that pre-existing target
and source bytes remain unchanged. Skip platform alias tests only when the
platform cannot create that alias.

Add successful alias-semantics tests:

- An in-directory symbolic-link target path becomes a regular merged file while
  the former referent retains its original bytes.
- A hardlink target path becomes the merged file while the other hardlink alias
  retains its original bytes.

Directly test `merge_json_files_with_reason()` for every exhaustive result:
`ok`, `invalid_path`, `missing_file`, `unreadable_file`, `same_file`,
`invalid_json`, `non_object_root`, and `write_failed`. Assert reason codes
contain no path string or JSON content. Also assert the Boolean wrapper returns
only the success item.

- [ ] **Step 3a: Run all helper tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_json_merge.py -q --basetemp=tests/.tmp/pytest-json-merge-validation-red
```

Expected: the validation, reason-code, formatting, alias, and atomic-write tests
all fail because the helper is still absent.

- [ ] **Step 4: Implement the minimal helper**

Implement this public API:

```python
def merge_json_files(first_json_path, second_json_path):
    ...

def merge_json_files_with_reason(first_json_path, second_json_path):
    ...
```

Implementation requirements:

0. Put the implementation in `merge_json_files_with_reason()`, returning
   `(success, reason_code)`, and make `merge_json_files()` return only the first
   tuple item. Use stable non-sensitive reason codes: `invalid_path`,
   `missing_file`, `unreadable_file`, `same_file`, `invalid_json`,
   `non_object_root`, and `write_failed`.
1. Convert accepted `str`/`os.PathLike` values with `os.fspath`; reject empty or
   non-string filesystem representations, then retain lexical absolute paths.
2. Require both lexical paths to be readable files.
3. Reject `os.path.samefile(first_path, second_path)`.
4. Load both files with `encoding="utf-8-sig"` and require `dict` roots.
5. Build `merged = dict(second_data)` followed by
   `merged.update(first_data)`.
6. Create a temporary file in the target directory with `tempfile.mkstemp`.
7. Write with `json.dump(..., indent=6, ensure_ascii=False)`, flush, and
   `os.fsync`.
8. Atomically replace the lexical target path with `os.replace`; do not replace
   a `realpath` value, so an in-root symlink entry is replaced rather than its
   former referent.
9. Catch expected path, JSON, and OS errors and return the matching
   `(False, reason_code)` from the detailed helper.
10. Close descriptors and remove an uncommitted temporary file in `finally`.

- [ ] **Step 5: Run focused tests and confirm GREEN**

Run:

```powershell
python -m pytest tests/test_json_merge.py -q --basetemp=tests/.tmp/pytest-json-merge-green
```

Expected: all helper tests pass.

- [ ] **Step 6: Commit Task 1**

```powershell
git add base/json_merge.py tests/test_json_merge.py
git commit -m "feat: add atomic JSON merge helper"
```

## Task 2: Make Configuration Backup And Restore Results Reliable

**Files:**
- Modify: `control/upgrade_config.py`
- Create: `tests/test_upgrade_config.py`

- [ ] **Step 1: Write failing backup state tests**

Build a `DummyLogger` and a fixture that monkeypatches
`control.upgrade_config` path constants into a temporary target/package tree.

Add tests proving:

- A successful managed-root copy changes only the matching backup flag to
  `False`.
- A `smart_copy()` return value of `False` makes `backup_config_file()` return
  `False` and leaves the matching flag `True`.
- One root is backed up only once when multiple files under it are requested.

- [ ] **Step 2: Write failing restore retention tests**

Add tests proving:

- Successful required restores return `True` and then delete backup data.
- Any `smart_copy()` failure makes `restore_config()` return `False`.
- On restore failure, `delete_backup_file()` is not called and all recovery
  files remain.
- All selected roots are attempted even if an earlier root restore fails.

- [ ] **Step 3: Run focused tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_upgrade_config.py -q --basetemp=tests/.tmp/pytest-upgrade-config-backup-red
```

Expected: failures because backup flags are set before copy success and restore
does not return/check results.

- [ ] **Step 4: Implement transactional backup flags**

Refactor `backup_config_file()` so it:

- Maps each requested target path to one existing managed root.
- Skips a root whose flag is already `False`.
- Creates `BACKUP_DIR` as needed.
- Calls `smart_copy(backup_source_path, backup_path)`.
- Sets the root flag to `False` only after `smart_copy()` returns `True`.
- Returns `False` immediately when a required backup cannot be made.
- Never logs a failed copy as a saved backup.

Keep the existing public method name and flag attributes.

- [ ] **Step 5: Implement checked restore and cleanup**

Make `restore_config()`:

- Attempt every root whose flag is `False`.
- Accumulate the Boolean results from `smart_copy`.
- If any restore copy fails, do not call `delete_backup_file`; return `False`
  and retain all backup roots.
- Call `delete_backup_file()` only when every restore copy succeeds.
- Return `True` after successful restore and cleanup.
- If post-restore cleanup partially fails, return `False`; successfully deleted
  backup roots may be gone, but each failed deletion target remains.

Make `delete_backup_file()` return a Boolean aggregate result and retain failed
deletion targets for later recovery while still attempting every selected root.

- [ ] **Step 6: Run focused and existing configuration-adjacent tests**

Run:

```powershell
python -m pytest tests/test_upgrade_config.py tests/test_upgrade_exe.py -q --basetemp=tests/.tmp/pytest-upgrade-config-backup-green
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 2**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "fix: make configuration rollback reliable"
```

## Task 3: Add Native `json_merge` YML Processing

**Files:**
- Modify: `control/upgrade_config.py`
- Modify: `tests/test_upgrade_config.py`

- [ ] **Step 1: Write failing schema and compatibility tests**

Add tests for:

- Missing `json_merge` and `json_merge: []` succeeding without helper calls.
- Existing `config` behavior still running before merge behavior.
- Non-list sections, non-dict entries, missing fields, empty fields, and unknown
  fields failing.
- Every malformed section/entry/unknown-field failure emits a concise error log
  that identifies the schema category without including JSON contents.
- A target also present in the same version's `config` list failing before a
  merge write.

Use real temporary version YML files through `Readyml`, not hand-built internal
dict calls only.

- [ ] **Step 2: Write failing path-safety tests**

Monkeypatch `DEFAULT_DIR`, `DEFAULT_TARGET_DIR`, `CONFIG_DIR`, and all managed
root constants to temporary directories.

Test rejection of:

- Absolute paths.
- Any original `..` component, including an in-root path such as
  `ui/ui_config/sub/../file.json` that would normalize back inside the root.
- Targets outside `UI_CONFIG_PATH`, `AI_MODEL_CONFIG_PATH`, or
  `SCANNER_BARCODE_CONFIG_PATH`.
- Sources outside `CONFIG_DIR`.
- Symlinks whose resolved destination escapes an allowed root.
- Lexical paths outside an allowed root whose symlink resolves inward.
- Missing target/source and non-file paths.

- [ ] **Step 3: Write failing ordered merge and rollback tests**

Create two version manifests and multiple source JSON files. Assert:

- Entries run in list order and versions run in `upgrade_version_list` order.
- The target-priority shallow merge result is correct.
- The source bytes never change.
- The managed root is backed up before the first write and only once.
- A later invalid merge returns `False`; calling `restore_config()` restores
  the target's exact pre-upgrade bytes.

- [ ] **Step 4: Run focused tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_upgrade_config.py -q --basetemp=tests/.tmp/pytest-yml-json-merge-red
```

Expected: new `json_merge` tests fail because the section is ignored.

- [ ] **Step 5: Implement schema normalization and safe resolution**

Add small private helpers to `ConfigManager`:

```python
def _normalize_json_merge_entries(self, raw_entries):
    ...

def _resolve_json_merge_target(self, relative_path):
    ...

def _resolve_json_merge_source(self, relative_path):
    ...
```

Requirements:

- Only relative non-empty strings are accepted.
- Inspect the original path components before normalization and reject every
  `..` component.
- Entry keys must be exactly `{"target", "source"}`.
- Build the lexical target with
  `os.path.abspath(os.path.join(DEFAULT_TARGET_DIR, target))`.
- Build the lexical source with
  `os.path.abspath(os.path.join(DEFAULT_DIR, source))`.
- Require both lexical containment and resolved containment: check the lexical
  absolute path against the lexical absolute allowed root, then use
  `os.path.realpath` plus `os.path.commonpath` for the second check.
- Return the normalized lexical target path for writing; use its `realpath`
  only for containment checks.
- Target containment uses the three rollback-managed roots.
- Source containment uses `CONFIG_DIR`.
- Require existing regular files.
- Reject target/source aliases using `os.path.samefile`.
- Return structured validation results rather than throwing expected errors.

- [ ] **Step 6: Implement ordered merge execution**

Add a private `_apply_json_merges(entries, config_entries)` method that:

1. Rejects a normalized target also listed in `config_entries`.
2. Resolves and validates both paths.
3. Calls `backup_config_file([target_relative_path])` before the first write.
4. Calls `merge_json_files_with_reason(target_path, source_path)`.
5. Logs/prints start, success, and the returned stable failure category without
   JSON content. Add tests for distinct `invalid_json`, `non_object_root`, and
   `write_failed` log categories.
6. Returns `False` on any failure.

Add a test in which `backup_config_file()` returns `False`; assert the detailed
merge helper is never called and both target/source bytes remain unchanged.

Add logger assertions for:

- Backup and restore copy failures.
- Unsafe and missing target/source paths.
- Merge start and success messages containing only the relative target/source
  paths.
- `invalid_json`, `non_object_root`, and `write_failed` reason categories with
  no JSON contents.

In `update_config_file()`, read `data.get("json_merge", [])` after ordinary
`config` processing for each version and call `_apply_json_merges`. Preserve
missing/empty-section compatibility.

- [ ] **Step 7: Run focused and regression tests**

Run:

```powershell
python -m pytest tests/test_json_merge.py tests/test_upgrade_config.py tests/test_upgrade_exe.py -q --basetemp=tests/.tmp/pytest-yml-json-merge-green
```

Expected: all tests pass.

- [ ] **Step 8: Commit Task 3**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "feat: support JSON merge upgrade steps"
```

## Task 4: Make Final Version Persistence Transactional

**Files:**
- Modify: `control/upgrade_db.py`
- Modify: `tests/test_upgrade_db.py`

- [ ] **Step 1: Update the successful persistence test**

Parameterize successful persistence over a legacy `audio_data.db` path and a
split `system_data.db` path. Assert:

```python
manager = DataBaseManager(str(system_path))
assert manager.update_version() is True
```

Keep assertions for `last_running_version`, `current_version`, and the changed
`update_time` value in both cases.

- [ ] **Step 2: Write failing read, execution, and commit rollback tests**

Use small connection proxies around a real SQLite connection to simulate:

- A valid table with no `current_version` row, causing
  `get_current_version()` to return `None`.
- The current-version `SELECT` raising `sqlite3.OperationalError`.
- One metadata `UPDATE` raising after an earlier update executed.
- `commit()` raising `sqlite3.OperationalError`.

Run relevant cases against both legacy audio and split system version database
fixtures.

Assert:

- `update_version()` returns `False`.
- `rollback()` was called.
- A new connection sees all three metadata rows unchanged.
- The connection was closed.

- [ ] **Step 3: Write a failing post-commit logging test**

Use a logger whose `info()` raises after the real commit, then independently
monkeypatch `builtins.print` to raise after a real commit. Assert for both:

- `update_version()` returns `True`.
- The committed metadata is present.

This test enforces that reporting failures cannot convert a committed
transaction into a reported upgrade failure.

- [ ] **Step 4: Run focused tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_upgrade_db.py -q --basetemp=tests/.tmp/pytest-version-transaction-red
```

Expected: return-value/rollback/logging tests fail against the current method.

- [ ] **Step 5: Implement the Boolean transaction**

Refactor `DataBaseManager.update_version()`:

```python
def update_version(self):
    committed = False
    try:
        current = self.get_current_version()
        if current is None:
            raise sqlite3.OperationalError("current version is unavailable")
        # execute all three UPDATE statements
        self.conn.commit()
        committed = True
    except sqlite3.Error as exc:
        self.conn.rollback()
        self.logger.error(...)
        return False
    finally:
        self.conn.close()

    try:
        print(...)
        self.logger.info(...)
    except Exception:
        pass
    return committed
```

Treat `None` from the existing error-swallowing `get_current_version()` as a
transaction failure before any metadata update. Do not catch programmer errors
around SQL construction as successful. Ensure read, execute, and commit
failures roll back before close.

- [ ] **Step 6: Run database tests and confirm GREEN**

Run:

```powershell
python -m pytest tests/test_upgrade_db.py -q --basetemp=tests/.tmp/pytest-version-transaction-green
```

Expected: all database upgrade tests pass.

- [ ] **Step 7: Commit Task 4**

```powershell
git add control/upgrade_db.py tests/test_upgrade_db.py
git commit -m "fix: make version persistence transactional"
```

## Task 5: Put Final Persistence And Rollback In One Orchestration Flow

**Files:**
- Modify: `main.py`
- Create: `tests/test_main.py`

- [ ] **Step 1: Write failing success-order test**

Add a test using monkeypatched manager classes/functions and an event list.
Call a new `run_upgrade()` function and assert the event order:

```text
backup_db
db_upgrade
config_update
exe_run
program_update
version_update
database_backup_cleanup
config_backup_cleanup
```

Assert `run_upgrade()` returns `True`.

Add post-commit cleanup cases where database-backup removal and
`delete_backup_file()` independently return `False` or raise. Assert:

- `run_upgrade()` still returns `True` because version persistence committed.
- No database/configuration restore is attempted.
- Both cleanup categories are attempted independently.
- Cleanup failures are logged and failed recovery files are left in place.

- [ ] **Step 2: Write failing rollback tests**

Parameterize failures from database upgrade, configuration/merge upgrade,
executable update, main-program update, and final version persistence.

For every pre-commit failure assert:

- Database restore is attempted.
- `ConfigManager.restore_config()` is attempted.
- Neither database nor configuration backups are deleted as success cleanup.
- `run_upgrade()` returns `False`.

Add a case where database restoration raises and assert configuration
restoration is still attempted. Assert database restoration exceptions and
`ConfigManager.restore_config()` returning `False` or raising are logged without
preventing the other restoration attempt.

Add filesystem integration cases using a real temporary target configuration
directory, real `ConfigManager` backup/merge/restore methods, and a real
`json_merge` manifest. After a successful merge, simulate failures at:

- Executable upgrade.
- Main-program update.
- Final version persistence.

For each case call `run_upgrade()` and assert the target JSON bytes exactly
equal the pre-upgrade bytes afterward. These tests complement the event-list
unit tests; mock-only verification that `restore_config()` was called is not
sufficient.

- [ ] **Step 3: Run focused tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_main.py -q --basetemp=tests/.tmp/pytest-main-rollback-red
```

Expected: import succeeds but `run_upgrade` is missing.

- [ ] **Step 4: Extract testable orchestration**

Create:

```python
def run_upgrade(logger=None):
    ...
```

Requirements:

- Create the logger when none is supplied.
- Return `False` immediately when initial database backup fails.
- Run each stage only when all prior stages succeeded.
- Call `DataBaseManager(get_version_database_path()).update_version()` only
  after program update succeeds and check its Boolean return.
- Delete the database backup and call `config_manager.delete_backup_file()` only
  after final persistence returns `True`.
- On any earlier failure or exception, attempt database restore and
  configuration restore in independent `try` blocks, then return `False`.
- Cleanup failures after final commit are logged, all cleanup categories are
  still attempted, recovery files are left where deletion failed, and
  `run_upgrade()` returns `True`; they do not run pre-commit rollback against a
  committed version.

Keep a small CLI wrapper under `if __name__ == "__main__":` that calls
`run_upgrade()`, pauses as the current Windows UX expects, and exits `0`/`1`.

- [ ] **Step 5: Run focused and complete regression tests**

Run:

```powershell
python -m pytest tests/test_main.py tests/test_upgrade_db.py tests/test_upgrade_config.py tests/test_upgrade_exe.py tests/test_json_merge.py tests/test_create_hardware_tables.py -q --basetemp=tests/.tmp/pytest-yml-json-merge-full
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 5**

```powershell
git add main.py tests/test_main.py
git commit -m "fix: unify upgrade rollback orchestration"
```

## Final Verification

- [ ] Run the complete test suite from the implementation worktree:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp=tests/.tmp/pytest-yml-json-merge-final
```

Expected: zero failures.

- [ ] Validate that all committed version manifests still parse:

```powershell
@'
from pathlib import Path
import yaml

for path in Path("resources").glob("version_*.yml"):
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    assert isinstance(data, dict), path
print("all version manifests parsed")
'@ | python -
```

Expected: `all version manifests parsed`.

- [ ] Review the final diff against
  `docs/double-sdd/specs/2026-07-27-json-file-merge-design.md` and confirm no
  version-specific YML, executable artifact, or unrelated dirty file changed.
