# Windows Missing Config Source Delete Compatibility Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On Windows, make a verified-missing ordinary-config source safely delete its managed destination and continue the upgrade; fail closed before mutation on non-Windows.

**Architecture:** Keep source classification, Windows native-handle helpers, preflight snapshots, and manifest orchestration in `control/upgrade_config.py`, as required by the approved design. Preflight records a stable source/destination snapshot before backup; the deletion helper later reacquires and compares the same objects, then binds source, destination, recovery directory, descendants, rename, cleanup, and recovery to verified Windows handles. Existing-source staged replacement remains unchanged.

**Tech Stack:** Python 3, ctypes Windows Native/Win32 APIs, pytest, PyYAML, PyInstaller

---

## File Structure

- Modify `control/upgrade_config.py`
  - verified lexical source classification;
  - lazy Windows API loading and small native handle helpers;
  - native handle ownership and reverse-order cleanup;
  - absolute and parent-handle-relative non-following opens;
  - handle identity, enumeration, reading, rename, disposition, and name-state
    primitives;
  - immutable pre-backup deletion snapshots;
  - exact-object compatibility deletion transaction;
  - operation preflight and delete-related conflict detection;
  - Windows platform/API gating;
  - backup/no-op rules;
  - transaction invocation and user-facing logging.
- Modify `tests/test_upgrade_config.py`
  - source classification, Windows-native handle, exact rename, cleanup,
    recovery, race, manifest integration, rollback, conflict, platform,
    logging, and no-mutation tests;
  - deterministic fake-adapter state-machine tests where an actual race cannot
    be scheduled reliably.
- Generate `dist/main.exe`
  - deliverable only; do not commit generated binary/build/cache/spec files.

## Task 1: Classify Verified-Missing Ordinary Sources

**Files:**
- Modify: `control/upgrade_config.py`
- Test: `tests/test_upgrade_config.py`

This task is unchanged from the first plan and may already be present on a
resumed implementation branch. If its commit and passing review are present,
verify rather than reimplement it.

- [ ] **Step 1: Write failing source-state tests**

Cover:

```python
def test_missing_ordinary_source_returns_candidate_and_missing_path(...):
    source_path, error = manager._resolve_ordinary_config_source(destination)
    assert source_path == expected_candidate
    assert error == "missing_path"


def test_dangling_or_indeterminate_source_is_not_missing(...):
    source_path, error = manager._resolve_ordinary_config_source(destination)
    assert source_path is None
    assert error in {"dangling_link", "identity_check_failed"}
```

Add direct resolver and end-to-end preflight coverage for dangling/reparse,
permission/identity failures, unsupported types, invalid paths, and source
escapes. Also cover a missing `resources` or `resources/config` parent and
assert it returns a fatal `missing_parent` category rather than `missing_path`.
Every non-`missing_path` result must fail before backup and preserve the target.

- [ ] **Step 2: Run focused tests and verify RED**

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp .pytest-tmp-task1-red `
  tests/test_upgrade_config.py -k 'ordinary_source'
```

Expected: new missing-source classification tests fail.

- [ ] **Step 3: Implement twice-confirmed lexical absence**

First verify every source-directory ancestor from the trusted updater root
through `resources/config`; a missing, replaced, reparse, or indeterminate
parent is fatal. Only after the expected parent directory is verified may the
final source basename be classified.

Add `_ordinary_config_source_entry_state()` so only:

```python
os.path.lexists(path) is False
# plus
os.lstat(path) raises FileNotFoundError
```

produces verified absence for the final basename beneath that verified parent.
A `FileNotFoundError` caused by a missing parent must be reported as
`missing_parent`, never `missing_path`. Dangling links, reparse points,
inconsistent observations, permissions, and unexpected I/O fail closed.

Return `(lexical_candidate, "missing_path")` only for verified absence.

- [ ] **Step 4: Run focused and full tests**

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp .pytest-tmp-task1-green `
  tests/test_upgrade_config.py -k 'ordinary_source'
python -m pytest -q --basetemp .pytest-tmp-task1-full
```

Expected: focused and complete suites pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "fix: classify missing ordinary config sources safely"
```

## Task 2: Build Windows Exact-Handle Primitives

**Files:**
- Modify: `control/upgrade_config.py`
- Test: `tests/test_upgrade_config.py`

- [ ] **Step 1: Write failing adapter availability and handle-owner tests**

Test that:

- importing the module is safe on every platform;
- `is_supported()` rejects non-Windows and missing APIs/filesystem support;
- every acquired handle is closed exactly once in reverse order;
- `BaseException` paths still close all handles;
- native failures expose non-sensitive categories, not raw paths or file
  contents.

- [ ] **Step 2: Write failing Windows-native primitive tests**

On Windows 10 1709+ use real temporary directories to verify:

- destination/source/recovery directories open with
  `FILE_SHARE_READ | FILE_SHARE_WRITE` and omit `FILE_SHARE_DELETE`;
- destination/source/recovery ancestor handles request
  `FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY`;
- handles use `FILE_FLAG_BACKUP_SEMANTICS |
  FILE_FLAG_OPEN_REPARSE_POINT`;
- target-root and descendant handles request only the required combination of
  `DELETE`, attribute, enumeration, and file-read rights, also omit
  `FILE_SHARE_DELETE`, and always include `FILE_FLAG_OPEN_REPARSE_POINT`;
- `FileIdInfo` returns volume serial plus 128-bit file ID;
- an open ancestor handle prevents rename/rebinding;
- `NtCreateFile` opens a child relative to a verified parent and does not follow
  a reparse point;
- final source absence distinguishes exact name-not-found statuses from access,
  parent, reparse, and unexpected failures;
- exact-handle rename resolves an artifact basename relative to the bound
  recovery-directory handle through `FILE_RENAME_INFO.RootDirectory`;
- replacement is disabled and collisions do not overwrite;
- `FileDispositionInfoEx` receives the delete, POSIX-semantics, and
  ignore-readonly flags;
- every disposed leaf handle is closed before cleanup proceeds to its parent;
- `FileDispositionInfoEx` deletes the exact opened object and unsupported
  API/filesystem responses fail without path fallback.

Use `pytest.mark.skipif(os.name != "nt", ...)` only for genuinely native tests.
Add deterministic structure/layout tests for ctypes definitions on non-Windows
so accidental ABI changes are still detected.

- [ ] **Step 3: Run Task 2 tests and verify RED**

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp .pytest-tmp-winhandles-red `
  tests/test_upgrade_config.py -k 'windows_handle'
```

Expected: failures because the Windows adapter does not exist.

- [ ] **Step 4: Implement lazy native bindings**

Implement small wrappers for:

```python
class WindowsDeleteUnsupported(Exception): ...
class WindowsDeleteFailure(Exception):
    category: str

class WindowsHandleOwner:
    def acquire(self, handle): ...
    def release(self, handle): ...
    def close_all(self): ...

class WindowsHandleOps:
    def is_supported(self): ...
    def open_absolute(self, path, *, directory, delete=False): ...
    def open_relative(self, parent_handle, name, *, directory, delete=False): ...
    def identity(self, handle): ...
    def enumerate(self, directory_handle): ...
    def read_digest(self, file_handle): ...
    def name_is_absent(self, parent_handle, name): ...
    def rename_relative_no_replace(
        self, object_handle, target_parent_handle, target_name
    ): ...
    def set_delete_disposition(self, handle): ...
    def close(self, handle): ...
```

Keep these helpers private to `control/upgrade_config.py`. Load
`kernel32`/`ntdll` only inside guarded Windows initialization. Define ctypes
argument/restype layouts explicitly and call `ctypes.get_last_error()`
immediately after failures.

Define the `FileDispositionInfoEx` flag constants explicitly. The disposition
wrapper must always combine delete, POSIX-semantics, and ignore-readonly for
compatibility cleanup. Cleanup callers must close each successfully disposed
descendant handle before processing its parent; no pathname-only fallback is
allowed.

- [ ] **Step 5: Implement identity-bound manifest primitives**

Build handle-derived manifest entries containing:

```python
(
    volume_serial,
    file_id_128,
    file_attributes,
    reparse_tag,
    size,
    last_write_time,
    digest_or_none,
)
```

Enumerate/open children relative to their parent handle, compare the opened
identity to the enumerated/captured identity, hash regular files from handles,
and never traverse reparse entries.

- [ ] **Step 6: Run native and full regression tests**

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp .pytest-tmp-winhandles-green `
  tests/test_upgrade_config.py -k 'windows_handle'
python -m pytest -q --basetemp .pytest-tmp-task2-full
python -m compileall -q control tests
git diff --check
```

Expected on the target Windows host: every Windows-native test executes and
passes; the complete suite passes.

- [ ] **Step 7: Commit Task 2**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "feat: add Windows exact-handle deletion primitives"
```

## Task 3: Implement the Windows Compatibility Deletion Transaction

**Files:**
- Modify: `control/upgrade_config.py`
- Test: `tests/test_upgrade_config.py`

- [ ] **Step 1: Write failing pre-backup snapshot tests**

Cover immutable preflight capture of:

- the trusted updater/source directory chain through `resources/config`;
- whether the destination and containing managed root exist;
- every existing destination ancestor identity from managed root to parent;
- destination identity/type and handle-derived stable content manifest;
- destination-root reparse rejection;
- recovery directory trust, volume identity, and native capability.

Assert missing source parents, destination identity/content failures, and
unsupported platform/API/filesystem states fail before backup or mutation.

- [ ] **Step 2: Write failing transaction success/no-op tests**

Cover:

- existing file target;
- existing directory tree target;
- target already absent;
- source appears at final bound check;
- destination root is a reparse point;
- recovery directory is outside the trusted updater root;
- destination and recovery directory are on different volumes;
- required API/filesystem support is missing.

Assert exact success, no-op, or fail-closed categories and handle close order.

- [ ] **Step 3: Write failing exact-object race tests**

Use native sharing semantics plus deterministic adapter boundaries to prove:

- swapping the target immediately before rename cannot redirect rename because
  the exact already-open handle is renamed;
- rebinding any destination ancestor fails while its no-delete-share handle is
  held;
- substituting `CONFIG_DIR` or a source ancestor cannot turn an existing source
  into an absent source;
- substituting `CONFIG_DIR`, a source ancestor, a destination ancestor, the
  target identity/type, or target content between preflight and mutation is
  rejected by comparison with the pre-backup snapshot;
- artifact-name collision cannot overwrite an entry and retries with a new
  collision-resistant basename; only a non-collision native error terminates
  the allocation loop;
- artifact name resolves relative to the held recovery-directory handle;
- descendant substitution between enumeration and relative open is rejected;
- substituting an artifact pathname cannot redirect exact-handle cleanup;
- unexpected child creation causes re-enumeration/disposition failure, never a
  false success.

- [ ] **Step 4: Write failing recovery and partial-cleanup tests**

Cover:

- failure before rename leaves the target untouched;
- post-rename manifest mismatch performs exact-handle no-clobber local restore;
- intact cleanup failure restores locally;
- partial cleanup retains the artifact under `BACKUP_DIR/recovery`;
- failed local restoration retains and reports the exact recovery path;
- recovery-artifact pathname substitution cannot satisfy local-restore checks;
- local restoration is attempted only while the captured exact artifact handle
  still matches its identity, the original destination is still absent, all
  verified ancestor handles remain held, and the no-clobber rename is relative
  to the bound destination-parent handle;
- global managed-root restore does not remove the external recovery artifact;
- if global rollback also fails, every remaining managed-root backup and
  recovery artifact is retained and each path is logged for manual recovery;
- root disposition/close/name-absence verification gates success logging;
- no failure path emits a delete-success message.

- [ ] **Step 5: Run transaction tests and verify RED**

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp .pytest-tmp-wintransaction-red `
  tests/test_upgrade_config.py -k 'delete_snapshot or transaction or race or recovery'
```

Expected: fail because the transaction does not exist.

- [ ] **Step 6: Implement preflight snapshot and deletion helper**

Add an immutable `_OrdinaryConfigDeleteSnapshot` and a preflight helper that
runs before backup. The snapshot records the exact source chain, destination
ancestor chain, destination existence/identity/type/stable manifest, managed
root state, recovery directory identity/volume, and capability result.

The deletion helper accepts the snapshot and must:

1. Reacquire and compare source, destination, and recovery ancestor chains to
   the pre-backup snapshot.
2. Reject missing-to-present, existing-to-absent, identity, type, and stable
   content changes before mutation.
3. Recheck final source absence relative to the held and snapshot-matched
   `CONFIG_DIR` handle.
4. Keep the snapshot-matched exact target handle open.
5. Rename that exact target handle to a collision-resistant name relative to the
   held recovery-directory handle without replacement.
6. Verify destination-name absence and artifact identity/manifest while keeping
   the same exact-object handle.
7. Recursively enumerate/open/validate/delete descendants leaf-first by handle.
8. Re-enumerate, arm `FileDispositionInfoEx` on the artifact root, close it,
   and verify both bound names absent before success.
9. Restore locally only while the same exact artifact handle matches the
   captured identity, the destination name remains absent, and verified
   ancestor handles remain held; perform a no-clobber handle-relative rename
   back to the bound destination parent. Otherwise retain/report the recovery
   artifact and return failure for global rollback.
10. Close all handles in reverse acquisition order on every path.

- [ ] **Step 7: Run focused, native race, and full suites**

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp .pytest-tmp-wintransaction-green `
  tests/test_upgrade_config.py -k 'delete_snapshot or transaction or race or recovery'
python -m pytest -q --basetemp .pytest-tmp-task3-full
python -m compileall -q control tests
git diff --check
```

Expected: all Windows-native transaction/race tests and the complete suite pass.

- [ ] **Step 8: Commit Task 3**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "feat: add Windows compatibility deletion transaction"
```

## Task 4: Integrate Deletion into Version Manifest Execution

**Files:**
- Modify: `control/upgrade_config.py`
- Modify: `tests/test_upgrade_config.py`

- [ ] **Step 1: Replace the old missing-source failure integration test**

On Windows assert:

```python
assert manager.update_config_file() is True
assert not target.exists()
assert backup_contains_original_target()
assert compatibility_delete_log_present()
assert missing_path_error_absent()
```

Cover managed-root directory deletion (`ui/ui_pic`), file deletion,
already-absent child, already-absent managed root, and absent descendant of an
absent managed root.

- [ ] **Step 2: Write failing platform/API/reparse integration tests**

Assert before backup/mutation:

- non-Windows returns `delete_compatibility:unsupported_platform`;
- missing Windows APIs/filesystem capability returns a safe category;
- destination-root symlink/junction/reparse returns
  `delete_compatibility:unsupported_destination_reparse`;
- a missing source parent returns `source_path:missing_parent`, not a delete
  operation;
- all non-`missing_path` source errors retain their current fatal behavior.

- [ ] **Step 3: Write failing conflict-preflight tests**

When a delete is present in a containing version manifest, reject before backup:

- normalized duplicate ordinary-config targets;
- delete/delete ancestor or descendant targets;
- delete/replacement ancestor or descendant targets;
- replacement/replacement ancestor or descendant targets whenever the same
  operation set contains at least one delete;
- identity-equivalent aliases for each of the preceding ordinary-config
  conflicts after normalization and identity resolution;
- delete equal to a JSON merge target;
- delete above a JSON merge target;
- delete below a JSON merge target.

Keep a replacement-only ordering regression proving existing behavior is
unchanged.

- [ ] **Step 4: Write failing backup/rollback/logging tests**

Cover:

- managed root exists: backup succeeds before transaction;
- target and managed root absent: no backup and backup flag unchanged;
- backup failure prevents transaction call;
- destination ancestor, target identity/type/content, or source-chain changes
  after preflight and before mutation fail against the snapshot without detach;
- transaction failure prevents JSON merges and later stages;
- later-stage failure invokes existing managed-root restore;
- retained `BACKUP_DIR/recovery` artifact survives global managed-root restore;
- success/no-op/failure/retained-artifact logs are distinct;
- failure never emits configuration delete success.

- [ ] **Step 5: Run integration tests and verify RED**

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp .pytest-tmp-integration-red `
  tests/test_upgrade_config.py -k 'missing_ordinary_source or compatibility_delete or delete_conflict'
```

Expected: fail because `update_config_file()` still reports missing source.

- [ ] **Step 6: Normalize and preflight operations**

Represent validated actions as:

```python
{
    "kind": "replace" or "delete",
    "relative_path": config_file_path,
    "destination_path": target_file_path,
    "source_path": source_candidate,
}
```

Only `source_error == "missing_path"` selects delete. Before backup:

- apply platform/API capability gating;
- invoke the native preflight helper and retain its immutable snapshot on the
  normalized delete operation;
- reject target-root reparse entries;
- reject delete-related ordinary and JSON merge equal/ancestor/descendant
  conflicts;
- preserve replacement-only sequencing.

- [ ] **Step 7: Invoke transaction after backup**

- Skip backup only when both target and containing managed root are recorded
  absent in the preflight snapshot.
- Otherwise use existing `backup_config_file`.
- Pass the pre-backup snapshot to the dedicated deletion helper.
- Reacquire and compare all snapshot identities/manifests, then revalidate
  source through the bound `CONFIG_DIR` handle immediately before mutation.
- Keep staged ordinary replacement unchanged.
- Fail immediately on transaction failure so top-level rollback runs.
- Apply JSON merges only after all ordinary actions succeed.

- [ ] **Step 8: Run focused and complete tests**

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp .pytest-tmp-integration-green `
  tests/test_upgrade_config.py -k 'ordinary_config or config_update or restore'
python -m pytest -q --basetemp .pytest-tmp-task4-full
python -m compileall -q base consts control load_yml log_model main.py tests
git diff --check
```

Expected: focused and complete suites pass.

- [ ] **Step 9: Commit Task 4**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "feat: restore Windows missing-source config deletion"
```

## Task 5: Full Verification and Windows Executable Packaging

**Files:**
- Verify: `control/upgrade_config.py`
- Verify: `tests/test_upgrade_config.py`
- Generate: `dist/main.exe`

- [ ] **Step 1: Run complete source verification**

```powershell
python -m compileall -q base consts control load_yml log_model main.py tests
$env:PYTHONPATH='.'
python -m pytest -q --basetemp .pytest-tmp-final
git diff --check
```

Expected: compile and all tests pass; Windows-native deletion/race tests execute
without platform skips on the packaging host.

- [ ] **Step 2: Audit implementation scope**

```powershell
git status --short
$implementationBase = git merge-base main HEAD
git diff "$implementationBase..HEAD" --stat
git log --oneline "$implementationBase..HEAD"
```

Expected tracked implementation scope:

- `control/upgrade_config.py`
- `tests/test_upgrade_config.py`

No generated files or unrelated user changes are committed.

- [ ] **Step 3: Build a clean one-file Windows executable**

```powershell
pyinstaller --noconfirm --clean --onefile --console --name main main.py
```

Expected: `dist/main.exe` exists and the build exits `0`.

- [ ] **Step 4: Record executable identity**

```powershell
Get-Item dist/main.exe | Select-Object FullName,Length,LastWriteTime
Get-FileHash -Algorithm SHA256 dist/main.exe
```

Record path, size, timestamp, and SHA-256 in the final handoff. Do not run the
new executable against the production installation during automated testing.

- [ ] **Step 5: Commit final source/test corrections only**

If verification required corrections:

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "test: verify Windows config deletion compatibility"
```

Do not commit `build/`, `dist/`, generated `.spec`, caches, backups, or temporary
test directories.
