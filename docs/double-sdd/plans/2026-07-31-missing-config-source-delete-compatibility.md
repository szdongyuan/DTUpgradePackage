# Missing Config Source Delete Compatibility Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the legacy-compatible behavior where a manifest `config` entry with a verified-missing package source safely deletes its managed destination and allows the upgrade to continue.

**Architecture:** Extend ordinary-config preflight to classify verified lexical source absence as a deletion operation while preserving all other source failures. Implement deletion as a destination-bound atomic rename to a recovery artifact followed by verified cleanup, then integrate it into the existing backup/rollback transaction. Existing-source staged replacement remains unchanged.

**Tech Stack:** Python 3, pytest, PyYAML, PyInstaller, Windows/POSIX filesystem primitives

---

## File Structure

- Modify `control/upgrade_config.py`
  - classify lexical source state;
  - normalize ordinary-config replacement/delete operations;
  - reject delete-related target conflicts;
  - implement bound atomic deletion and recovery;
  - integrate deletion into `update_config_file()`.
- Modify `tests/test_upgrade_config.py`
  - add source-state, deletion, race, recovery, rollback, conflict, and logging
    regression tests;
  - replace the old expectation that every missing ordinary source is fatal.
- Build `dist/main.exe`
  - generated deliverable only; do not commit the binary unless explicitly
    requested.

## Task 1: Classify Verified-Missing Ordinary Sources

**Files:**
- Modify: `control/upgrade_config.py:2040-2151`
- Test: `tests/test_upgrade_config.py:1430-1478`
- Test: `tests/test_upgrade_config.py:3225-3315`

- [ ] **Step 1: Write failing source-state tests**

Add focused tests proving:

```python
def test_missing_ordinary_source_is_verified_lexical_absence(...):
    source_path, error = manager._resolve_ordinary_config_source(destination)
    assert source_path == expected_missing_source
    assert error == "missing_path"


def test_dangling_ordinary_source_is_not_delete_compatible(...):
    source_path, error = manager._resolve_ordinary_config_source(destination)
    assert source_path is None
    assert error == "dangling_link"


def test_indeterminate_ordinary_source_identity_is_not_missing(...):
    ...
    assert error == "identity_check_failed"
```

Cover directory and file source candidates, dangling symlinks, junction/reparse
points where supported, and injected `lexists`/`lstat` failures.

Also add end-to-end `update_config_file()` tests for dangling/reparse,
permission/indeterminate, unsupported-type, invalid-path, and symlink-escape
source results. For every non-`missing_path` category, assert:

```python
assert manager.update_config_file() is False
assert backup_calls == []
assert snapshot_tree(target) == original_target_tree
assert not any("按兼容模式删除" in message for message in logged_messages(manager))
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp tests/.tmp/source-state `
  tests/test_upgrade_config.py -k 'ordinary_source and (missing or dangling or indeterminate)'
```

Expected: new tests fail because missing sources currently return no candidate
path and dangling/indeterminate source states are not fully distinguished.

- [ ] **Step 3: Implement lexical source-state classification**

Add a helper alongside `_destination_entry_state`:

```python
def _ordinary_config_source_entry_state(self, path):
    # Return (False, None) only when lexists is false and lstat confirms
    # FileNotFoundError. Return dangling_link for an existing lexical entry
    # whose followed target is absent. Return identity_check_failed for
    # indeterminate I/O/identity results.
```

Update `_resolve_ordinary_config_source()` so verified absence returns the
expected in-root lexical candidate plus `missing_path`:

```python
if source_exists is False:
    return lexical_path, "missing_path"
if state_error is not None:
    return None, state_error
```

Preserve lexical containment and resolved-path containment checks before
classifying the source as delete-compatible. Existing supported sources still
return `(lexical_path, None)`.

- [ ] **Step 4: Run focused and existing source-security tests**

Run:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp tests/.tmp/source-state-pass `
  tests/test_upgrade_config.py -k 'ordinary_source'
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "fix: classify missing ordinary config sources safely"
```

## Task 2: Implement Destination-Bound Atomic Compatibility Deletion

**Files:**
- Modify: `control/upgrade_config.py:1284-1482`
- Modify: `control/upgrade_config.py:1920-2011`
- Test: `tests/test_upgrade_config.py` near existing ordinary-config replacement race tests

- [ ] **Step 1: Write failing atomic-deletion tests**

Add direct helper tests for:

- existing managed directory detaches and is fully removed;
- existing managed file detaches and is fully removed;
- an absent destination is a successful no-op;
- source appearance immediately before mutation fails closed;
- existing-target identity/content/type changes are rejected;
- absent-to-present destination creation is rejected;
- ancestor symlink/junction substitution cannot mutate an external target;
- atomic detach failure preserves the destination;
- post-detach verification failure restores only a verified artifact;
- artifact substitution prevents local restoration and retains the artifact;
- cleanup failure locally restores a still-intact artifact;
- partial cleanup preserves the remaining artifact and returns failure;
- absent-destination no-op logs a no-op message and never logs deletion
  failure;
- retained recovery artifacts log their exact path;
- every deletion failure logs failure and does not log deletion success;
- every acquired ancestor handle is released on success, failure, and
  `BaseException`.

Use existing helpers such as `snapshot_tree`, `swap_artifacts`,
`_ordinary_config_destination_binding`, `_atomic_move_no_replace`, and the
replacement-race test patterns around
`test_ordinary_config_concurrent_destination_change_is_preserved`.

- [ ] **Step 2: Run atomic-deletion tests and verify they fail**

Run:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp tests/.tmp/delete-helper `
  tests/test_upgrade_config.py -k 'ordinary_config_compatibility_delete'
```

Expected: fail because the deletion helper does not exist.

- [ ] **Step 3: Implement artifact verification and recovery helpers**

Add focused helpers that reuse the existing ordinary-config binding model:

```python
def _ordinary_config_artifact_matches(
    self,
    artifact_path,
    expected_manifest,
):
    ...


def _recover_deleted_ordinary_config(
    self,
    artifact_path,
    destination_path,
    initial_binding,
    expected_manifest,
):
    # Reacquire safe ancestor handles, require destination absence,
    # revalidate the artifact, then atomically move with no replacement.
    ...
```

Never restore an artifact whose identity/stable manifest cannot be proven to be
the detached destination.

- [ ] **Step 4: Implement `_delete_missing_source_ordinary_config`**

The helper accepts the source candidate, destination, and preflight binding:

```python
def _delete_missing_source_ordinary_config(
    self,
    source_path,
    destination_path,
    initial_binding,
):
    ...
```

Required order:

1. Revalidate the destination through
   `_ordinary_config_destination_is_current`.
2. Prove the expected source remains lexically absent.
3. Acquire and retain ancestor-chain handles.
4. Require `_ordinary_config_binding_matches(...)`.
5. Return success immediately for a verified absent destination.
6. Reserve a sibling `.<name>.delete-*` artifact path.
7. Recheck source absence and bindings.
8. Atomically move destination to the artifact with no replacement.
9. Verify destination absence and artifact stable manifest.
10. Clean the artifact with `_cleanup_snapshot_artifact` so ordinary-config
    deletion follows the existing snapshot-artifact cleanup rules.
11. On failure, perform only fully revalidated no-clobber local recovery;
    otherwise retain/log the artifact and return `False`.
12. Release handles in `finally`.

Do not call `smart_remove(destination_path)` directly.

- [ ] **Step 5: Run deletion-helper and existing replacement-race tests**

Run:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp tests/.tmp/delete-helper-pass `
  tests/test_upgrade_config.py -k 'ordinary_config_compatibility_delete or ordinary_config_concurrent or ordinary_config_recovery'
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 2**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "feat: add atomic ordinary config compatibility deletion"
```

## Task 3: Integrate Missing-Source Deletion into Manifest Execution

**Files:**
- Modify: `control/upgrade_config.py:2438-2646`
- Test: `tests/test_upgrade_config.py:1392-1545`
- Test: `tests/test_upgrade_config.py` near rollback and alias-preflight tests

- [ ] **Step 1: Replace the old missing-source failure test**

Change `test_missing_ordinary_source_fails_before_backup_or_target_mutation`
into end-to-end compatibility tests proving:

```python
assert manager.update_config_file() is True
assert not target.exists()
assert backup_contains_original_target()
assert "按兼容模式删除" in "\n".join(logged_messages(manager))
assert "source_path:missing_path" not in "\n".join(
    logged_messages(manager, "error")
)
```

Add cases for:

- `ui/ui_pic` managed-root directory deletion;
- managed file deletion;
- already-absent child no-op with normal root backup;
- already-absent managed-root no-op without changing the root backup flag;
- existing source still uses staged replacement;
- backup failure prevents deletion;
- deletion helper failure stops before JSON merges;
- calling `restore_config()` after a later simulated failure restores the exact
  deleted target tree;
- recovery-artifact substitution causes the deletion helper to reject local
  restoration, after which the top-level failure path invokes
  `restore_config()` and restores the exact managed-root backup while retaining
  and logging the substituted artifact;
- successful flow leaves the backup available for normal top-level cleanup.

For success, no-op, and failure cases, assert the required log separation:

```python
assert successful_delete_has_explicit_compatibility_message()
assert absent_noop_has_explicit_noop_message()
assert failure_has_explicit_failure_message()
assert failure_does_not_have_success_message()
assert retained_artifact_log_contains_exact_path()
```

- [ ] **Step 2: Add failing delete-related conflict tests**

Within one manifest, require preflight failure before backup when a
compatibility deletion conflicts with:

- the same target through a normalized alias;
- a replacement descendant;
- a replacement ancestor.

Also retain a replacement-only duplicate/ordering test to prove existing
replacement semantics are unchanged.

- [ ] **Step 3: Run integration tests and verify they fail**

Run:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp tests/.tmp/delete-integration `
  tests/test_upgrade_config.py -k 'missing_ordinary_source or compatibility_delete or delete_conflict'
```

Expected: fail because `update_config_file()` still reports `missing_path`.

- [ ] **Step 4: Normalize ordinary-config operations during preflight**

Represent every validated entry as a small immutable tuple/dict containing:

```python
{
    "relative_path": config_file_path,
    "destination_path": target_file_path,
    "source_path": source_candidate,
    "kind": "replace" or "delete",
    "destination_binding": initial_binding,
}
```

Only `source_error == "missing_path"` produces `kind == "delete"`. All other
source errors keep the existing fatal reporting.

When any operation in the version is a delete, reject normalized/samefile and
ancestor/descendant conflicts involving that delete before backup.

- [ ] **Step 5: Execute deletion after backup**

After successful `backup_config_file()`:

- log that the missing source selected compatibility deletion;
- call `_delete_missing_source_ordinary_config` for delete operations;
- keep `_replace_with_staged_ordinary_config` for replacement operations;
- fail immediately on deletion/replacement failure;
- preserve existing ordering before JSON merge actions.

For a verified-absent managed-root delete no-op, bypass the impossible snapshot
of that absent root and leave its backup flag unchanged. Do not generalize this
exception to replacement operations.

- [ ] **Step 6: Run focused integration and rollback tests**

Run:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp tests/.tmp/delete-integration-pass `
  tests/test_upgrade_config.py -k 'ordinary_config or config_update or restore'
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 3**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "feat: restore missing-source config deletion compatibility"
```

## Task 4: Full Verification and Executable Packaging

**Files:**
- Verify: `control/upgrade_config.py`
- Verify: `tests/test_upgrade_config.py`
- Verify: complete test suite
- Generate: `dist/main.exe`

- [ ] **Step 1: Run syntax compilation**

Run:

```powershell
python -m compileall -q base consts control load_yml log_model main.py
```

Expected: exit code `0`.

- [ ] **Step 2: Run the complete test suite**

Run:

```powershell
$env:PYTHONPATH='.'
python -m pytest -q --basetemp tests/.tmp/full-delete-compatibility
```

Expected: all tests pass, with only environment-dependent symlink/junction
tests skipped when unsupported.

- [ ] **Step 3: Verify working-tree scope**

Run:

```powershell
git status --short
git diff --check
git diff 00f2e78..HEAD --stat
```

Expected: implementation commits modify only
`control/upgrade_config.py` and `tests/test_upgrade_config.py`; no unrelated
user changes are included.

- [ ] **Step 4: Build a clean one-file executable**

Run from the implementation worktree:

```powershell
pyinstaller --noconfirm --clean --onefile --console --name main main.py
```

Expected: `dist/main.exe` exists and PyInstaller exits `0`.

- [ ] **Step 5: Verify the generated executable identity**

Run:

```powershell
Get-Item dist/main.exe | Select-Object FullName,Length,LastWriteTime
Get-FileHash -Algorithm SHA256 dist/main.exe
```

Record the absolute path, size, build timestamp, and SHA-256 in the handoff.
Do not run the executable against the user's production installation during
automated verification.

- [ ] **Step 6: Commit any final source/test-only corrections**

If verification required source or test corrections, commit only those files:

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py
git commit -m "test: verify missing-source deletion compatibility"
```

Do not commit `build/`, `dist/`, generated `.spec`, cache, backup, or temporary
test directories.
