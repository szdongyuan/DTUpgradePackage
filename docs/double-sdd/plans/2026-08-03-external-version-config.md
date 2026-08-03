# External Version Configuration Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load the ordered upgrade-version sequence and target version from an editable external `resources/version.yml`, honor the declared order safely, prevent version downgrades, and assemble the file beside the PyInstaller executable.

**Architecture:** Add a focused `consts.version_config` module for YAML loading, validation, runtime-root resolution, and index-based version selection. `consts.program_config` loads one validated snapshot and retains the existing `ALL_VERSIONS`/`TARGET_VERSION` compatibility surface; the upgrade consumers share the selection helpers. The main orchestrator adds a read-only compatibility preflight, while a PowerShell wrapper turns `pyinstaller -F main.py` plus external resources into a verified staged release directory.

**Tech Stack:** Python 3.12, PyYAML, SQLite, pytest, PowerShell, PyInstaller.

---

## File Structure

- Create `consts/version_config.py`: version configuration exceptions, version-file root resolution, YAML validation, and index-based selection helpers.
- Create `resources/version.yml`: external source of truth containing the current sequence and target.
- Create `tests/test_version_config.py`: focused loader, validation, path-resolution, and selection tests.
- Modify `consts/program_config.py`: remove hard-coded sequence/target and load one external snapshot while preserving public constants.
- Modify `tests/test_version_0_26_07_02.py`: assert the release contract through the external file.
- Modify `control/upgrade_config.py`: derive configuration upgrade versions through the shared index-based helper.
- Modify `control/upgrade_db.py`: use shared selection, reuse the legacy initial-version constant, and guard persistence from unknown-version writes or downgrade.
- Modify `main.py`: perform the mutation-free unknown/newer-version preflight before backup.
- Modify `tests/test_upgrade_config.py`: verify configured order is used instead of lexical comparison.
- Modify `tests/test_upgrade_db.py`: verify database selection, legacy initialization, unknown-version persistence rejection, and no-downgrade persistence.
- Modify `tests/test_main.py`: verify newer/unknown preflight behavior and update existing orchestration fixtures for the configured sequence.
- Create `scripts/build_release.ps1`: run `pyinstaller -F main.py`, safely stage `main.exe` with the external `resources` tree, and validate staged `version.yml`.
- Create `tests/test_build_release.py`: exercise the real staging branch with isolated fake build inputs and verify failure behavior.

### Task 1: Add the validated external version model

**Files:**
- Create: `consts/version_config.py`
- Create: `resources/version.yml`
- Create: `tests/test_version_config.py`

- [ ] **Step 1: Write failing loader and validation tests**

Create `tests/test_version_config.py`. Use `tmp_path` to write YAML inputs and cover:

```python
def test_load_version_config_preserves_declared_order(tmp_path):
    path = tmp_path / "version.yml"
    path.write_text(
        'versions: ["0.12", "10", "2"]\ntarget_version: "2"\n',
        encoding="utf-8",
    )

    versions, target = load_version_config(path)

    assert versions == ["0.12", "10", "2"]
    assert target == "2"
```

Add parameterized failures for:

- missing file;
- a file containing invalid UTF-8 bytes;
- an unreadable file simulated by monkeypatching `Path.read_text` to raise
  `PermissionError`, so the result is deterministic on Windows and CI;
- malformed YAML;
- scalar/list top-level document instead of a mapping;
- missing or empty `versions`;
- non-string, empty, whitespace-padded, `.`/`..`, slash, backslash, NUL, and other path-unsafe entries;
- duplicate versions;
- omission of `LEGACY_INITIAL_VERSION` (`0.12`);
- missing, non-string, empty, whitespace-padded, unsafe, or unlisted `target_version`.

Each assertion must expect `VersionConfigError`, and the message must contain both the input path and a stable reason fragment without exposing a traceback as the user-facing diagnostic. The invalid-byte and unreadable-file cases must prove that `UnicodeDecodeError` and `OSError` subclasses are wrapped rather than escaping raw.

- [ ] **Step 2: Write failing runtime-root and sequence tests**

Test a pure version-file root helper with explicit arguments so it does not depend on the test process. This helper is only for locating the new external version file; it must not redefine existing database, backup, config, or log roots:

```python
assert resolve_application_dir(
    module_file=repo / "consts" / "program_config.py",
    executable=tmp_path / "python.exe",
    frozen=False,
) == repo

assert resolve_application_dir(
    module_file=tmp_path / "_MEI" / "consts" / "program_config.py",
    executable=tmp_path / "package" / "main.exe",
    frozen=True,
) == tmp_path / "package"
```

Add selection tests using a lexically misleading sequence and a nonterminal target:

```python
versions = ["0.12", "10", "2", "3"]
assert select_upgrade_versions(versions, "2", "0.12") == ["10", "2"]
assert select_upgrade_versions(versions, "2", "10") == ["2"]
assert select_upgrade_versions(versions, "2", "2") == []
assert select_upgrade_versions(versions, "2", "3") == []
```

Also assert that `compare_version_positions(...)` returns before/equal/after results from indices and that both helpers raise `UnknownVersionError` for an unknown current version or invalid target.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_version_config.py
```

Expected: collection fails because `consts.version_config` does not exist.

- [ ] **Step 4: Implement the minimal loader and selection module**

Create `consts/version_config.py` with these public interfaces:

```python
LEGACY_INITIAL_VERSION = "0.12"

class VersionConfigError(ValueError):
    pass

class UnknownVersionError(VersionConfigError):
    pass

def resolve_application_dir(module_file, executable, frozen): ...
def load_version_config(path): ...
def compare_version_positions(versions, target_version, current_version): ...
def select_upgrade_versions(versions, target_version, current_version): ...
```

Implementation constraints:

- use `yaml.safe_load`, UTF-8, and `pathlib.Path`;
- accept only identifiers matching `[A-Za-z0-9][A-Za-z0-9._-]*`, while explicitly rejecting `.` and `..`;
- reject rather than trim whitespace;
- return a new mutable `list` for compatibility and the target `str`;
- require unique versions, membership of target, and membership of `0.12`;
- use `list.index`, never string comparison or sorting;
- wrap file/YAML/schema failures in `VersionConfigError` with the path and clear reason, preserving the original exception as `__cause__` where appropriate;
- in frozen mode return the executable parent; in source mode return the repository root derived from the module path.

- [ ] **Step 5: Add the external configuration file**

Create `resources/version.yml` with exactly the sequence formerly in `consts/program_config.py`:

```yaml
versions:
  - "0.12"
  - "0.25.06.01"
  - "0.25.07.01"
  - "0.25.09.01"
  - "0.25.12.01"
  - "0.26.02.01"
  - "0.26.06.01"
  - "0.26.07.01"
  - "0.26.07.02"
target_version: "0.26.07.02"
```

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_version_config.py
python -m py_compile consts/version_config.py tests/test_version_config.py
git diff --check
```

Expected: all focused tests and checks pass.

- [ ] **Step 7: Commit the external version model**

```powershell
git add -f -- consts/version_config.py resources/version.yml tests/test_version_config.py
git commit -m "feat: add external version configuration model"
```

### Task 2: Load the external snapshot through the existing constants API

**Files:**
- Modify: `consts/program_config.py:1-30`
- Modify: `tests/test_version_0_26_07_02.py`
- Modify: `tests/test_version_config.py`

- [ ] **Step 1: Write failing compatibility-boundary tests**

Update the release contract test so it reads `resources/version.yml` and verifies that it is the runtime source:

```python
def test_external_version_file_defines_current_release():
    path = Path(__file__).resolve().parents[1] / "resources" / "version.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["versions"] == ALL_VERSIONS
    assert data["target_version"] == TARGET_VERSION == "0.26.07.02"
```

Add a subprocess test in `tests/test_version_config.py` that changes the working directory away from the repository, imports `consts.program_config` with the repository on `PYTHONPATH`, and asserts `VERSION_CONFIG_PATH`, `ALL_VERSIONS`, and `TARGET_VERSION` resolve from the repository source root. Explicitly assert that `DEFAULT_DIR` still follows its existing `sys.argv[0]` derivation; the new resolver must not change downstream database, backup, config, or log locations. Add a second subprocess case that copies the source `consts` package without `resources/version.yml` and asserts import fails with a diagnostic naming `version.yml`; there must be no hard-coded fallback.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_version_config.py tests/test_version_0_26_07_02.py
```

Expected: at least the missing-file/no-fallback test fails because production constants are still hard-coded.

- [ ] **Step 3: Replace hard-coded values in `program_config.py`**

At the start of `consts/program_config.py`:

- import `Path`, `load_version_config`, and `resolve_application_dir`;
- leave `DEFAULT_DIR`, `DEFAULT_TARGET_DIR`, and every existing downstream path calculation unchanged;
- derive a separate version-configuration root from the repository module location in source mode and from `sys.executable` when `sys.frozen` is true;
- define `VERSION_CONFIG_PATH = version_config_root / "resources" / "version.yml"`;
- assign `ALL_VERSIONS, TARGET_VERSION = load_version_config(VERSION_CONFIG_PATH)` once;
- delete the literal Python version sequence and target.

Do not catch `VersionConfigError` in this module: invalid external configuration must stop startup before managers or mutation.

- [ ] **Step 4: Run focused and import regression tests**

Run:

```powershell
python -m pytest -q tests/test_version_config.py tests/test_version_0_26_07_02.py tests/test_upgrade_db.py::test_version_database_path_uses_audio_database_for_legacy_single_db
python -m py_compile consts/program_config.py tests/test_version_0_26_07_02.py
git diff --check
```

Expected: all tests and checks pass.

- [ ] **Step 5: Commit the compatibility integration**

```powershell
git add -- consts/program_config.py tests/test_version_config.py tests/test_version_0_26_07_02.py
git commit -m "feat: load version constants from external yaml"
```

### Task 3: Honor configured order and prevent unknown-version writes or downgrade

**Files:**
- Modify: `control/upgrade_config.py:17,3339-3350`
- Modify: `control/upgrade_db.py:1-10,168-175,218-299`
- Modify: `main.py:1-15,145-195`
- Modify: `tests/test_upgrade_config.py`
- Modify: `tests/test_upgrade_db.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Write failing consumer-order tests**

In `tests/test_upgrade_config.py`, construct a minimal manager via `ConfigManager.__new__`, set `logger`, `current_version`, and `upgrade_version_list`, monkeypatch module constants to:

```python
ALL_VERSIONS = ["0.12", "10", "2", "3"]
TARGET_VERSION = "2"
```

Call `init_upgrade_version_list()` and assert the result is `["10", "2"]`; this must fail under the current lexical comparisons.

In `tests/test_upgrade_db.py`, use a temporary SQLite database, stub `update_db` and `check_table_data`, configure the same misleading order, run `upgrade("0.12")`, and assert `update_db` receives `"10"` then `"2"` only.

- [ ] **Step 2: Write failing no-downgrade and unknown-version persistence tests**

Add two `DataBaseManager.update_version()` tests using a target in the middle of the list:

```python
ALL_VERSIONS = ["0.12", "1", "2", "3"]
TARGET_VERSION = "2"
```

- current `3`: returns `True`, closes the connection, and leaves all three metadata rows byte-for-byte unchanged;
- current `unknown`: returns `False`, closes the connection, and leaves all metadata unchanged.

Keep the existing rollback/reporting semantics for genuine SQLite errors.

- [ ] **Step 3: Write failing orchestration preflight tests**

Update `install_fake_upgrade()` in `tests/test_main.py` to monkeypatch `upgrade_main.ALL_VERSIONS` and `upgrade_main.TARGET_VERSION` to a sequence containing its normal fake current value. Then add:

- known current after target: `run_upgrade()` returns `True`; events contain no backup, restore, manager construction, program update, persistence, or cleanup;
- unknown non-empty current: `run_upgrade()` returns `False`; events contain no backup, restore, manager construction, program update, persistence, or cleanup; logger contains a compatibility diagnostic;
- missing current (`None`): continues into the pre-existing backup-first path, preserving the existing unavailable-metadata behavior.

- [ ] **Step 4: Run the new tests and verify RED**

Run exact new test node IDs after naming them, for example:

```powershell
python -m pytest -q `
  tests/test_upgrade_config.py::test_upgrade_versions_follow_configured_order_not_lexical_order `
  tests/test_upgrade_db.py::test_database_upgrade_follows_configured_order_not_lexical_order `
  tests/test_upgrade_db.py::test_update_version_does_not_downgrade_newer_current_version `
  tests/test_upgrade_db.py::test_update_version_rejects_unknown_current_version_without_mutation `
  tests/test_main.py::test_newer_installed_version_is_mutation_free_successful_noop `
  tests/test_main.py::test_unknown_installed_version_fails_before_backup_or_restore
```

Expected: failures demonstrate lexical selection, unconditional backup, and unconditional target persistence.

- [ ] **Step 5: Integrate shared index-based selection**

Modify `control/upgrade_config.py` so `init_upgrade_version_list()` replaces its loop/comparisons with:

```python
self.upgrade_version_list.extend(
    select_upgrade_versions(ALL_VERSIONS, TARGET_VERSION, self.current_version)
)
```

Modify `control/upgrade_db.py` so `upgrade()` uses the same helper. Import and use `LEGACY_INITIAL_VERSION` in `create_system_info_table()` instead of duplicating the `0.12` literal. Preserve connection closing, transaction behavior, diagnostics, and `check_table_data()` behavior.

- [ ] **Step 6: Add orchestration preflight and persistence guards**

Import `ALL_VERSIONS`, `TARGET_VERSION`, `UnknownVersionError`, and `compare_version_positions` in `main.py`. After logger setup but before `DataBaseManager.backup_db()`:

```python
current_version = get_current_version()
if current_version not in (None, ""):
    try:
        relation = compare_version_positions(
            ALL_VERSIONS, TARGET_VERSION, current_version
        )
    except UnknownVersionError as exc:
        _log_safely(logger, "error", f"版本兼容性检查失败: {exc}")
        return False
    if relation > 0:
        _log_safely(logger, "info", "当前版本高于升级包目标版本，无需升级")
        return True
```

Reuse the preflight `current_version` later rather than reading it twice. The unknown/newer branches must return outside the general exception handler so `_restore_after_failure()` is never invoked.

In `DataBaseManager.update_version()`, classify the stored current version before issuing any `UPDATE`:

- unknown current: report, close, return `False` without rollback-dependent mutations;
- current after target: report, close, return `True` with metadata untouched;
- equal/before target: keep the existing transactional persistence behavior.

- [ ] **Step 7: Run focused consumer and orchestration suites**

Run:

```powershell
python -m pytest -q tests/test_upgrade_config.py tests/test_upgrade_db.py tests/test_main.py
python -m py_compile main.py control/upgrade_config.py control/upgrade_db.py
git diff --check
```

Expected: all suites and checks pass, including all existing rollback and recovery tests.

- [ ] **Step 8: Commit ordered selection and guards**

```powershell
git add -- main.py control/upgrade_config.py control/upgrade_db.py tests/test_main.py tests/test_upgrade_config.py tests/test_upgrade_db.py
git commit -m "fix: honor configured version order safely"
```

### Task 4: Assemble and verify the external configuration in release output

**Files:**
- Create: `scripts/build_release.ps1`
- Create: `tests/test_build_release.py`

- [ ] **Step 1: Write failing release-assembly tests**

Create `tests/test_build_release.py`. Each test builds isolated inputs under a unique directory below repository `dist/test-release-<uuid>` so the script's destructive-clean safety boundary can be exercised without touching the workspace root. Invoke PowerShell with `subprocess.run` and `-NoProfile`.

The success test supplies:

- `-SkipBuild`;
- a fake `main.exe` path containing marker bytes;
- a temporary resources source containing a valid `version.yml` plus a marker subdirectory/file;
- a unique stage directory under `dist`.

Assert exit code `0`, exact executable bytes, copied external YAML, copied marker content, and that `load_version_config(stage / "resources" / "version.yml")` succeeds.

Add a deterministic non-skip build test. Create a temporary fake
`pyinstaller.ps1` that records every received argument and writes a fake
executable to the path named by a test-only environment variable. Invoke
`build_release.ps1` without `-SkipBuild`, pass the fake script through
`-PyInstallerExecutable`, and point `-ExecutableSource` at that generated file.
Assert the recorder contains exactly `-F` followed by `main.py`, the wrapper ran
from the repository root, and the generated executable was staged. This test is
mandatory and does not depend on a real PyInstaller installation.

After staging, start a Python subprocess with the repository on `PYTHONPATH`, set
`sys.frozen = True` and `sys.executable` to the staged `main.exe`, then import
`consts.program_config`. Assert the production `VERSION_CONFIG_PATH` equals the
staged `resources/version.yml` path and its public constants match that file.
This verifies the actual frozen-mode production lookup rather than only calling
the loader with an explicit path.

Add failures for:

- missing source `version.yml`;
- missing executable;
- a stage path equal to or outside the repository `dist` directory.

All tests must clean only their own resolved `dist/test-release-<uuid>` directory in `finally`.

- [ ] **Step 2: Run the assembly tests and verify RED**

Run:

```powershell
python -m pytest -q tests/test_build_release.py
```

Expected: failure because `scripts/build_release.ps1` does not exist.

- [ ] **Step 3: Implement the safe PyInstaller wrapper**

Create `scripts/build_release.ps1` with parameters equivalent to:

```powershell
param(
    [string]$PyInstallerExecutable = "pyinstaller",
    [string]$PythonExecutable = "python",
    [string]$ExecutableSource,
    [string]$ResourcesSource,
    [string]$StageDirectory,
    [switch]$SkipBuild
)
```

Required behavior:

1. Resolve the repository root from `$PSScriptRoot` and default the stage to `dist/UpgradePackage`.
2. Resolve and validate that the stage is a strict descendant of the repository `dist` directory before any recursive removal.
3. Unless `-SkipBuild`, run exactly `pyinstaller -F main.py` from the repository root and fail on a nonzero exit code.
4. Default the executable source to `dist/main.exe` and resources source to repository `resources`.
5. Fail before build or cleaning/staging if source `resources/version.yml` is absent. For `-SkipBuild`, also require the executable up front; for a real/fake build, validate the executable immediately after PyInstaller returns successfully.
6. Clean only the validated stage directory, then copy `main.exe` and the external resources tree.
7. Verify staged `main.exe` and `resources/version.yml` exist.
8. Invoke the Python loader against the staged YAML with `PythonExecutable`; fail if validation fails.
9. Print the absolute staged artifact path on success.

Do not use `--add-data`; the YAML must remain editable beside the executable.

- [ ] **Step 4: Run release-assembly tests and verify GREEN**

Run:

```powershell
python -m pytest -q tests/test_build_release.py
git diff --check
```

Expected: all tests pass; the deterministic recorder proves the non-skip branch invoked exactly `pyinstaller -F main.py`; frozen-mode production import resolves the staged YAML; and no files outside each test's validated stage directory or explicit fake executable/recorder paths are changed.

- [ ] **Step 5: Smoke-test the actual build entry point when PyInstaller is available**

Run:

```powershell
Get-Command pyinstaller
powershell -NoProfile -File scripts/build_release.ps1
Test-Path dist/UpgradePackage/main.exe
Test-Path dist/UpgradePackage/resources/version.yml
python -c "from consts.version_config import load_version_config; print(load_version_config(r'dist/UpgradePackage/resources/version.yml'))"
```

Expected: command discovery succeeds; the script runs `pyinstaller -F main.py`; both paths exist; the staged YAML loads successfully. If `pyinstaller` is unavailable in the active environment, record that exact environment limitation and still require the isolated `-SkipBuild` tests to pass.

- [ ] **Step 6: Commit the release assembly**

```powershell
git add -f -- scripts/build_release.ps1 tests/test_build_release.py
git commit -m "build: stage external version configuration"
```

### Task 5: Run complete regression and acceptance verification

**Files:**
- Verify all files changed by Tasks 1-4.

- [ ] **Step 1: Verify no production hard-coded sequence or target remains**

Run:

```powershell
rg -n "ALL_VERSIONS\s*=\s*\[|TARGET_VERSION\s*=\s*['\"]0\.26\.07\.02" consts control main.py
```

Expected: no production-code match. Test fixtures and `resources/version.yml` may contain literal release values.

- [ ] **Step 2: Run all focused acceptance suites**

Run:

```powershell
python -m pytest -q tests/test_version_config.py tests/test_version_0_26_07_02.py tests/test_upgrade_config.py tests/test_upgrade_db.py tests/test_main.py tests/test_build_release.py
```

Expected: all focused tests pass.

- [ ] **Step 3: Run the full regression suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass, with only already-known environment-dependent skips.

- [ ] **Step 4: Run static and diff checks**

Run:

```powershell
python -m py_compile consts/version_config.py consts/program_config.py control/upgrade_config.py control/upgrade_db.py main.py
git diff --check
git status --short
```

Expected: compilation and whitespace checks pass; status contains only intentional implementation files plus any pre-existing unrelated user changes.

- [ ] **Step 5: Commit any verification-only corrections**

If verification required code or test corrections, stage only the files owned by this plan and commit them:

```powershell
git add -- consts/version_config.py consts/program_config.py resources/version.yml control/upgrade_config.py control/upgrade_db.py main.py scripts/build_release.ps1 tests/test_version_config.py tests/test_version_0_26_07_02.py tests/test_upgrade_config.py tests/test_upgrade_db.py tests/test_main.py tests/test_build_release.py
git commit -m "test: verify external version configuration"
```

If no files changed after the prior commits, do not create an empty commit.
