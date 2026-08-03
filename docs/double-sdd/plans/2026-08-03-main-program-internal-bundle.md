# Main Program `_internal` Bundle Replacement Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `DiTing.exe` and an optional sibling `_internal` directory as one recoverable filesystem bundle while preserving packages that do not contain `_internal`.

**Architecture:** Add a focused `control.program_bundle` transaction module that validates fixed source objects, stages complete replacements beside the deployed program, commits both objects, and restores displaced targets on failure. `main.update_version_program()` keeps its existing tuple return contract and delegates filesystem work to this module while retaining best-effort reporting.

**Tech Stack:** Python 3.12 standard library (`dataclasses`, `os`, `pathlib`, `shutil`, `tempfile`), pytest.

---

## File Structure

- Create `control/program_bundle.py`: resolved-parent path handling, source validation, no-link staging copy, paired commit, rollback, and artifact cleanup.
- Create `tests/test_program_bundle.py`: direct filesystem transaction and injected-race/failure coverage.
- Modify `main.py`: call the bundle transaction for fixed source/destination paths and report skip/success/failure.
- Modify `tests/test_main.py`: main-stage integration and orchestration contract coverage.

### Task 1: Validate, stage, and commit successful bundle replacements

**Files:**
- Create: `control/program_bundle.py`
- Create: `tests/test_program_bundle.py`

- [ ] **Step 1: Write failing path, classification, and success tests**

Define the intended public interface in tests:

```python
result = replace_program_bundle(
    source_executable,
    destination_executable,
    source_internal,
    destination_internal,
    warning_reporter=None,
)

assert result.internal_replaced is True
```

Cover:

- Input paths use resolved real parent directories plus the original fixed leaf name. Resolving a parent must not follow an existing destination leaf link/junction.
- Missing source `_internal` returns `internal_replaced=False`, replaces the executable, and leaves destination `_internal` unchanged when it is a directory, file, symlink, broken link, or junction.
- Present real source `_internal` returns `True`, installs beside the executable, and fully replaces an existing destination directory including removal of stale target-only files.
- With participating source `_internal`, existing destination `_internal` objects that are files, symlinks, broken links, or junctions are displaced as objects and replaced without traversal.
- Existing destination `DiTing.exe` objects that are a regular file, directory, symlink, broken link, or junction are displaced as objects and replaced by the new regular file.
- Missing, directory, symlink, broken-link, junction, and unreadable/non-regular executable sources fail before destination mutation.
- Source `_internal` that is a file, symlink, broken link, or junction fails before destination mutation.
- Nested source links/junctions and other non-regular entries such as FIFO, socket, or device objects fail before destination mutation. Tests may construct the object on supporting platforms or inject a no-follow directory-entry/stat result on Windows.
- A source entry changed to a link/junction after initial validation but before its staging recheck is rejected without following the substituted object. Tests do not simulate mutation in the final check-to-use instant because the approved spec requires a stable source package during one serialized invocation.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest -q --basetemp=.pytest-tmp-program-bundle-red tests/test_program_bundle.py`

Expected: FAIL because `control.program_bundle` does not exist.

- [ ] **Step 3: Implement resolved paths, validation, no-link copying, and success commit**

Add:

```python
class ProgramBundleError(Exception):
    pass


@dataclass(frozen=True)
class ProgramBundleResult:
    internal_replaced: bool
```

Path rules:

- Convert every input to an absolute path by applying `os.path.realpath()` to its parent directory and then rejoining the unchanged leaf name.
- Require both destination paths to resolve to the same parent and fixed distinct leaves.
- Never call `realpath()` on a destination leaf, so existing leaf symlinks/junctions remain objects that can be displaced.

Validation/copy rules:

- Use `os.path.lexists()` to distinguish broken links from missing paths.
- Reject source links using `os.path.islink()` and junctions using `pathlib.Path.is_junction()` when available.
- Walk source `_internal` with `os.scandir()` and `follow_symlinks=False`.
- Do not use `shutil.copytree()` for source traversal. Recheck each directory immediately before `scandir()`, recheck every entry during staging, and never pass a source link to a following copy API.
- Pin each source file with an opened descriptor, compare `os.fstat()` type/identity with `os.stat(path, follow_symlinks=False)`, stream bytes from the descriptor into staging, then apply metadata best effort. Directory traversal rechecks immediately before enumeration under the stable-source precondition.

Commit rules:

- Stage every participating source completely before destination displacement.
- Move existing destination leaf objects to unique sibling recovery paths without traversing them.
- Install staged `_internal` only when participating, then install the staged executable.
- On success remove recovery/staging objects created by the invocation. Post-commit cleanup failures call `warning_reporter` best effort and retain only the exact failed artifact.

- [ ] **Step 4: Run success tests and verify GREEN**

Run: `python -m pytest -q --basetemp=.pytest-tmp-program-success tests/test_program_bundle.py -k "path or success or missing or invalid or link or junction or stale or destination"`

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- control/program_bundle.py tests/test_program_bundle.py
git commit -m "feat: stage main program bundle"
```

### Task 2: Add rollback, artifact reporting, and control-flow safety

**Files:**
- Modify: `control/program_bundle.py`
- Modify: `tests/test_program_bundle.py`

- [ ] **Step 1: Write failing rollback and cleanup tests**

Inject failures at executable/directory staging, both displacement operations, both installation operations, removal of newly installed objects, restoration of displaced objects, pre-commit staging cleanup, and post-commit recovery cleanup.

Assertions:

- A normal failure leaves both original destination objects intact, or raises `ProgramBundleError` naming every retained staging/recovery artifact when complete restoration/cleanup is impossible.
- Partial staging is removed on copy failure; cleanup failure retains and reports the exact staging path.
- Recovery failure never deletes the only recoverable prior object.
- Recovery/staging symlinks, broken links, and junctions are removed as leaf objects; cleanup never traverses their referents. Parameterized tests verify external link/junction target contents remain unchanged after successful cleanup and rollback cleanup.
- Artifacts created by the invocation are absent when cleanup succeeds.
- With source `_internal` absent, destination `_internal` remains untouched through executable staging, displacement, installation, and rollback failures for directory, file, symlink, broken-link, and junction destination forms.
- `KeyboardInterrupt` and `SystemExit` injected at staging, displacement, installation, rollback, and cleanup boundaries trigger all remaining best-effort recovery/cleanup and then propagate the first encountered control-flow exception unchanged. When an ordinary primary failure is followed by the first control-flow exception during recovery, that control-flow exception propagates after remaining recovery attempts; secondary recovery exceptions never replace an earlier control-flow signal.

- [ ] **Step 2: Run rollback tests and verify RED**

Run: `python -m pytest -q --basetemp=.pytest-tmp-program-rollback-red tests/test_program_bundle.py -k "failure or rollback or cleanup or interrupt or system_exit or absent_source"`

Expected: newly added failure injections expose incomplete behavior.

- [ ] **Step 3: Implement explicit transaction state and recovery**

Track staged, displaced, and installed objects separately. Catch `BaseException` only around the transaction boundary to perform reverse-order rollback/cleanup. Convert only `Exception` instances to `ProgramBundleError`; preserve and re-raise the first non-`Exception` control-flow signal after all remaining recovery attempts. Cleanup helpers classify leaf objects: unlink files/links/broken links, remove junction leaves without entering referents, and recursively delete only known real directories. They operate only on exact invocation-owned sibling artifacts and never traverse external referents or an undisplaced destination leaf.

- [ ] **Step 4: Run complete direct tests**

Run: `python -m pytest -q --basetemp=.pytest-tmp-program-bundle-final tests/test_program_bundle.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```powershell
git add -- control/program_bundle.py tests/test_program_bundle.py
git commit -m "fix: recover main program bundle replacement"
```

### Task 3: Integrate the bundle transaction with the upgrade orchestrator

**Files:**
- Modify: `main.py:1-28`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Write failing main-stage integration tests**

Cover:

- Fixed paths passed to `replace_program_bundle()` are `resources/DiTing.exe`, sibling `resources/_internal`, and deployed sibling destinations.
- `ProgramBundleResult(internal_replaced=False)` logs an informational source-missing skip and preserves `(True, "更新主程序成功")`; `True` logs directory replacement success. `main.py` does not independently inspect the source path.
- The `warning_reporter` argument is wired to a best-effort main-stage reporter that calls `_print_safely(message)` and `_log_safely(logger, "warning", message)`. A simulated post-commit cleanup warning containing a retained artifact path reaches the log without changing the successful return tuple.
- `ProgramBundleError` preserves `(False, "更新主程序失败: ...")`, including retained staging/recovery paths in the error log, and `run_upgrade()` treats it as a required-stage failure.
- Console/logger failures do not alter the transaction result.
- `KeyboardInterrupt` and `SystemExit` are not swallowed.

- [ ] **Step 2: Run integration tests and verify RED**

Run: `python -m pytest -q --basetemp=.pytest-tmp-program-main-red tests/test_main.py -k "program or internal or bundle"`

Expected: FAIL until `main.py` delegates to the new module.

- [ ] **Step 3: Modify `update_version_program()` minimally**

Import `replace_program_bundle`, `ProgramBundleError`, and `ProgramBundleResult`. Pass fixed paths and a warning callback that independently uses `_print_safely()` plus `_log_safely(logger, "warning", ...)`; use the result to log skip/replacement, catch ordinary transaction failures for the existing false tuple, and allow `BaseException` to propagate. Continue using `_print_safely()` and `_log_safely()` so reporting remains best effort.

- [ ] **Step 4: Run focused, relevant, and full verification**

```powershell
python -m pytest -q --basetemp=.pytest-tmp-program-integration tests/test_program_bundle.py tests/test_main.py
python -m py_compile control/program_bundle.py main.py tests/test_program_bundle.py tests/test_main.py
git diff --check
python -m pytest -q --basetemp=.pytest-tmp-program-full
```

Expected: all tests and checks pass, with only existing environment-dependent skips.

- [ ] **Step 5: Commit Task 3**

```powershell
git add -- main.py tests/test_main.py
git commit -m "feat: upgrade main program internal bundle"
```
