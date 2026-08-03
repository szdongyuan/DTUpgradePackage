# Missing Ordinary Config Source Skip Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore replacement-only ordinary configuration behavior so a missing package source is skipped without touching its target, while retaining valid replacements and JSON merge.

**Architecture:** Keep the current upgrade, backup, rollback, staged replacement, and JSON merge infrastructure. Change ordinary-operation construction in `ConfigManager.update_config_file`: classify manifest syntax and package source before destination resolution, omit missing-leaf entries, and construct replacement-only operations for present sources. In `_replace_with_staged_ordinary_config`, bind the original source through staging copy and staged-content validation; after that boundary, commit the validated snapshot without consulting the live source or triggering source-driven rollback. Leave the Windows deletion machinery dormant to avoid a risky broad refactor.

**Tech Stack:** Python 3.12, pytest, PyInstaller, Windows filesystem APIs

---

## File Structure

- `control/upgrade_config.py`: ordinary configuration source-first operation construction, skip reporting, and the validated-snapshot boundary in `_replace_with_staged_ordinary_config`.
- `tests/test_upgrade_config.py`: focused behavior, race, backup-isolation, and JSON merge regression coverage.
- `tests/test_main.py`: top-level upgrade success/rollback expectation for missing ordinary sources.

### Task 1: Restore source-first replacement-only behavior

**Files:**
- Modify: `control/upgrade_config.py` in `ConfigManager.update_config_file` and `_replace_with_staged_ordinary_config`
- Modify: `tests/test_upgrade_config.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Add failing focused regression tests**

Add tests that construct manifests with a real installed `ui/ui_pic` target but no `resources/config/ui_pic` source. Assert:

```python
assert manager.update_config_file() is True
assert snapshot_tree(ui_pic_target) == original_tree
assert no ordinary backup, replacement, or delete helper was called for "ui/ui_pic"
assert "delete_compatibility selected" not in logged output
```

Add a mixed manifest test with missing `ui/ui_pic` and present `ui/ui_config/mic_channel_calibration.json`; assert the picture tree is unchanged and the calibration file is replaced.

Add a JSON merge test where an ordinary source is missing; assert the ordinary target remains unchanged and JSON keys are appended successfully.

Add cases for:

- invalid non-string/empty/NUL/rooted/absolute/drive-qualified entries with absent basenames remain errors;
- `.` and internal `sub/..` aliases with missing derived sources skip, while aliases with present sources continue through destination containment validation;
- missing `resources/config` parent remains fatal;
- a source appearing after missing classification remains skipped;
- a source disappearing or changing identity before staged-snapshot validation completes fails without changing the target;
- a source changing after staged-snapshot validation does not trigger rollback, and the committed target equals the validated original staged snapshot rather than the later source object.

Update top-level orchestration coverage so missing ordinary sources do not trigger database/config rollback.

- [ ] **Step 2: Run new tests and verify RED**

Run exact new test node IDs with a workspace-local `--basetemp`. Expected failures must show current missing-source deletion selection or upgrade failure behavior.

- [ ] **Step 3: Implement source-first classification**

In `update_config_file`, for each ordinary manifest entry:

```python
path_error = self._ordinary_config_path_error(config_file_path)
if path_error is not None:
    self._report_ordinary_config_destination_error(path_error)
    return False

source_path, source_error = self._resolve_ordinary_config_source(
    config_file_path
)
if source_error == "missing_path":
    self._report_best_effort(
        "info",
        f"配置源不存在，跳过: {config_file_path}",
    )
    continue
if source_error is not None:
    self._report_ordinary_config_source_error(source_error)
    return False

destination_path, path_error = self._resolve_ordinary_config_destination(
    config_file_path
)
```

Construct only `kind == "replace"` operations. Do not call delete conflict preflight, delete preflight, or delete execution from the active flow. Pass replacement operations to JSON conflict preflight, back up only their relative paths, and preserve source identity/content revalidation through staged-snapshot validation. After the staged snapshot is validated, treat it as the immutable transaction input and do not trigger rollback solely because the live package source changes.

In `_replace_with_staged_ordinary_config`, keep the original expected-state checks at helper entry, before copy, after copy, and through `_ordinary_config_staging_is_safe` plus the stable staged manifest. Remove the later live-source checks from destination commit and recovery transitions. Add tests that change the live source after staged validation and assert a successful commit of the original validated staged bytes for both existing and absent targets.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the new regression nodes, existing ordinary replacement/backup/rollback tests, JSON merge tests, and top-level orchestration tests. Expected: all selected tests pass; only privilege-dependent Windows symlink tests may skip.

- [ ] **Step 5: Run full verification**

Run:

```powershell
python -m pytest -q --basetemp .pytest-tmp-missing-source-skip
python -m py_compile control\upgrade_config.py tests\test_upgrade_config.py tests\test_main.py
git diff --check
```

Expected: zero failures and clean compile/diff checks.

- [ ] **Step 6: Commit implementation**

```powershell
git add control/upgrade_config.py tests/test_upgrade_config.py tests/test_main.py
git commit -m "fix: skip missing ordinary config sources"
```

### Release Verification (orchestrator after reviewed merge)

- [ ] Stop only running `main.exe` processes whose executable path is exactly
  `C:\Users\Administrator\Desktop\20260417\20260701-1\main.exe`.
- [ ] Preserve the existing deployed executable under a timestamped `.bak` name.
- [ ] Build with `python -m PyInstaller --clean --noconfirm main.spec` from the merged main worktree so the user's uncommitted `0.26.07.01` configuration is included.
- [ ] Copy `dist/main.exe` to the deployment directory.
- [ ] Verify the embedded archive contains the source-first skip message, JSON merge module, and target version `0.26.07.01`, and does not call the missing-source delete branch from `update_config_file`.
- [ ] Execute the deployed upgrade non-interactively while capturing stdout/stderr and exit code; require a zero exit code and successful overall upgrade outcome.
- [ ] Verify no `delete_compatibility selected`, no backup error for `ui/ui_pic`, no rollback caused by the missing source, and the installed `ui_pic` tree remains unchanged.
- [ ] Report executable SHA-256 and preserve logs and the prior executable backup.
