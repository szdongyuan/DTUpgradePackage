# EXE Launch Upgrade Implementation Plan

> **For agentic workers:** REQUIRED: Use the `subagent-driven-development` skill to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add version-YAML driven executable launch support to the upgrade flow while preserving old YAML behavior.

**Architecture:** Add a focused `ExeUpgradeManager` in `control/upgrade_exe.py` that reads `exe` entries for the already-computed upgrade versions, validates and normalizes entries, runs processes sequentially with `subprocess.run`, and returns a boolean result. `main.py` wires this manager between config upgrade and main program replacement. Tests cover manager behavior without depending on real Windows `.exe` files.

**Tech Stack:** Python standard library (`os`, `subprocess`, `dataclasses` or simple dicts), existing `Readyml.load_yaml`, existing `LogManager`, and `pytest` for focused tests.

---

## Files

- Create: `control/upgrade_exe.py`
  - Owns YAML `exe` entry normalization, path validation, process execution, terminal/log output, and aggregate success/failure result.
- Modify: `main.py`
  - Imports and invokes `ExeUpgradeManager` after config upgrade succeeds and before `update_version_program`.
- Create: `tests/test_upgrade_exe.py`
  - Focused pytest coverage for empty entries, shorthand entries, invalid fields, required/optional failures, and command execution.

## Task 1: Add Executable Upgrade Manager

**Files:**
- Create: `control/upgrade_exe.py`
- Test: `tests/test_upgrade_exe.py`

- [ ] **Step 1: Create failing tests for no-op and normalization**

Add `tests/test_upgrade_exe.py` with tests that import `ExeUpgradeManager` and exercise private/public helper behavior through `run()` where possible.

Suggested starting tests:

```python
import os
import sys

from control.upgrade_exe import ExeUpgradeManager


class DummyLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))


def test_empty_upgrade_versions_returns_success(tmp_path):
    manager = ExeUpgradeManager(DummyLogger(), [], base_dir=str(tmp_path))
    assert manager.run() is True


def test_missing_exe_section_returns_success(tmp_path):
    version_file = tmp_path / "resources" / "version_1.0.yml"
    version_file.parent.mkdir()
    version_file.write_text("version: '1.0'\nconfig: []\n", encoding="utf-8")

    manager = ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(tmp_path))

    assert manager.run() is True
```

- [ ] **Step 2: Run tests and confirm they fail**

Run:

```bash
python -m pytest tests/test_upgrade_exe.py -v
```

Expected: FAIL because `control.upgrade_exe` does not exist.

- [ ] **Step 3: Implement manager skeleton and no-op behavior**

Create `control/upgrade_exe.py` with:

```python
import os
import subprocess

from consts.program_config import DEFAULT_DIR
from load_yml.load_yml import Readyml


class ExeUpgradeManager(object):
    def __init__(self, logger, upgrade_version_list, base_dir=None):
        self.logger = logger
        self.upgrade_version_list = upgrade_version_list or []
        self.base_dir = os.path.realpath(base_dir or DEFAULT_DIR)

    def run(self):
        print("正在处理升级可执行程序...")
        self._log_info("正在处理升级可执行程序...")
        for upgrade_version in self.upgrade_version_list:
            target_version_path = os.path.join(self.base_dir, "resources", f"version_{upgrade_version}.yml")
            data = Readyml.load_yaml(target_version_path) or {}
            exe_entries = data.get("exe") or []
            if not exe_entries:
                continue
            if not isinstance(exe_entries, list):
                self._log_error("exe 配置必须是列表")
                return False
            if not self._run_entries(exe_entries, upgrade_version):
                return False
        return True
```

Include `_log_info`, `_log_warning`, and `_log_error` helpers that tolerate `logger` being `None`.

- [ ] **Step 4: Run tests and confirm no-op tests pass**

Run:

```bash
python -m pytest tests/test_upgrade_exe.py -v
```

Expected: existing tests PASS.

- [ ] **Step 5: Add failing tests for successful execution, required failure, and optional failure**

Add tests that create a lightweight Python script and call it through `sys.executable`:

```python
def write_script(path, body):
    path.write_text(body, encoding="utf-8")
    return path


def test_required_command_success_returns_success(tmp_path):
    script = write_script(tmp_path / "ok.py", "import sys\nprint('hello exe')\n")
    version_file = tmp_path / "resources" / "version_1.0.yml"
    version_file.parent.mkdir(exist_ok=True)
    version_file.write_text(
        "version: '1.0'\n"
        "exe:\n"
        f"  - path: {sys.executable!r}\n"
        f"    args: [{str(script)!r}]\n",
        encoding="utf-8",
    )

    manager = ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(tmp_path))

    assert manager.run() is True


def test_required_nonzero_command_fails(tmp_path):
    script = write_script(tmp_path / "fail.py", "import sys\nsys.exit(7)\n")
    version_file = tmp_path / "resources" / "version_1.0.yml"
    version_file.parent.mkdir(exist_ok=True)
    version_file.write_text(
        "version: '1.0'\n"
        "exe:\n"
        f"  - path: {sys.executable!r}\n"
        f"    args: [{str(script)!r}]\n",
        encoding="utf-8",
    )

    manager = ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(tmp_path))

    assert manager.run() is False


def test_optional_nonzero_command_does_not_fail(tmp_path):
    script = write_script(tmp_path / "fail.py", "import sys\nsys.exit(7)\n")
    version_file = tmp_path / "resources" / "version_1.0.yml"
    version_file.parent.mkdir(exist_ok=True)
    version_file.write_text(
        "version: '1.0'\n"
        "exe:\n"
        f"  - path: {sys.executable!r}\n"
        f"    args: [{str(script)!r}]\n"
        "    required: false\n",
        encoding="utf-8",
    )

    manager = ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(tmp_path))

    assert manager.run() is True
```

- [ ] **Step 6: Implement normalization, validation, and process execution**

Implement:

- `_run_entries(exe_entries, upgrade_version)` loops sequentially and stops on the first required failure.
- `_normalize_entry(entry)` returns `(config, error, required)` where `config` includes `name`, `path`, `args`, `cwd`, `timeout`, and `required`.
- String entries become `{"path": entry}`.
- Non-dict/non-string entries are malformed required failures.
- Dict field rules exactly match the spec:
  - `path` must be a non-empty string.
  - invalid `name` falls back to `path`.
  - `args` must be a list of strings, integers, or floats and each item is converted to a string.
  - `cwd` must be a non-empty string resolving to an existing directory.
  - `timeout` must be a positive int/float.
  - invalid `required` defaults to `True`.
- `_resolve_path(value, must_stay_in_base)` resolves relative paths from `self.base_dir`, uses `os.path.realpath`, and rejects relative paths escaping `self.base_dir`.
- Absolute executable paths and cwd paths are allowed.
- Executable `path` must exist and be a file.
- Default `cwd` is the executable parent directory and must be a directory.
- Launch with:

```python
result = subprocess.run(
    [config["path"]] + config["args"],
    cwd=config["cwd"],
    timeout=config["timeout"],
    capture_output=True,
    text=True,
    shell=False,
)
```

Print and log start, stdout, stderr, success exit code, non-zero exit code, timeout, and start exceptions in Chinese, consistent with existing style.

- [ ] **Step 7: Run tests and confirm process behavior passes**

Run:

```bash
python -m pytest tests/test_upgrade_exe.py -v
```

Expected: all current tests PASS.

- [ ] **Step 8: Add failing tests for invalid fields and path containment**

Add tests for:

- Missing required `path` fails.
- Missing optional `path` is logged/skipped and does not fail.
- Plain string `args` fails.
- Relative path using `../` that escapes `base_dir` fails.
- Explicit nonexistent `cwd` fails.
- Successful explicit `cwd` is honored. Use a Python script that prints `os.getcwd()` and assert captured terminal output contains the expected directory.
- Positive `timeout` is honored by using a quick script with `timeout: 5`.
- Required timeout failure returns `False` by using a script that sleeps longer than a very small positive timeout.
- Optional timeout failure logs/prints the timeout and returns `True`.
- String shorthand launches successfully when the shorthand points to `sys.executable` and `args` are not needed, or verify shorthand normalization through a helper if direct launch would be unhelpful.
- Terminal output is emitted for start, successful end, stdout, stderr, non-zero exit, timeout, and start failure. Use `capsys` to assert the relevant Chinese labels/fragments are printed.
- Logger output is emitted for start, successful end, stdout, stderr, non-zero exit, timeout, and start failure. Use `DummyLogger.messages` to assert the same lifecycle categories are logged.

- [ ] **Step 8b: Add focused output tests**

Add or refine tests so output expectations are explicit:

```python
def logged_text(logger):
    return "\n".join(message for _level, message in logger.messages)


def test_success_prints_and_logs_start_end_stdout_and_stderr(tmp_path, capsys):
    script = write_script(
        tmp_path / "talk.py",
        "import sys\nprint('out text')\nprint('err text', file=sys.stderr)\n",
    )
    version_file = tmp_path / "resources" / "version_1.0.yml"
    version_file.parent.mkdir(exist_ok=True)
    version_file.write_text(
        "version: '1.0'\n"
        "exe:\n"
        "  - name: Talker\n"
        f"    path: {sys.executable!r}\n"
        f"    args: [{str(script)!r}]\n",
        encoding="utf-8",
    )
    logger = DummyLogger()

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(tmp_path)).run() is True

    printed = capsys.readouterr().out
    logs = logged_text(logger)
    for text in ["启动可执行程序", "可执行程序执行结束", "out text", "err text"]:
        assert text in printed
        assert text in logs
```

Also include one test each for:

- Non-zero exit output includes the display name and exit code.
- Timeout output includes the display name and timeout seconds.
- Start failure output includes the display name/path and exception text. Trigger this by creating a file with invalid executable content and configuring that file as the absolute `path`, so `subprocess.run` raises a stable process start exception instead of relying on platform-specific permission behavior.

- [ ] **Step 8c: Add explicit cwd tests**

Add:

```python
def test_explicit_cwd_is_used(tmp_path, capsys):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    script = write_script(tmp_path / "cwd.py", "import os\nprint(os.getcwd())\n")
    version_file = tmp_path / "resources" / "version_1.0.yml"
    version_file.parent.mkdir(exist_ok=True)
    version_file.write_text(
        "version: '1.0'\n"
        "exe:\n"
        f"  - path: {sys.executable!r}\n"
        f"    args: [{str(script)!r}]\n"
        f"    cwd: {str(work_dir)!r}\n",
        encoding="utf-8",
    )

    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(tmp_path)).run() is True

    assert str(work_dir) in capsys.readouterr().out
```

- [ ] **Step 9: Fix validation gaps**

Update `control/upgrade_exe.py` until all validation, timeout, cwd, print, and log tests pass.

- [ ] **Step 10: Commit manager and tests**

Run:

```bash
git add control/upgrade_exe.py tests/test_upgrade_exe.py
git commit -m "feat: add upgrade exe runner"
```

## Task 2: Wire Executable Runner Into Main Upgrade Flow

**Files:**
- Modify: `main.py`
- Test: `tests/test_upgrade_exe.py`

- [ ] **Step 1: Add a failing integration-style test for main flow wiring if practical**

If `main.py` can be tested without executing the whole upgrade flow, add a small helper function test. If not practical because `main.py` is script-oriented, document that wiring is verified by static import/compile and direct manager tests.

Do not refactor the full script into a new architecture just for this feature.

- [ ] **Step 2: Modify `main.py` to invoke `ExeUpgradeManager`**

Add:

```python
from control.upgrade_exe import ExeUpgradeManager
```

Then update the success gate:

```python
result_exe = False
result_program = False
if result_db and result_config and result_ui:
    exe_manager = ExeUpgradeManager(logger, config_manager.upgrade_version_list)
    result_exe = exe_manager.run()
if result_db and result_config and result_ui and result_exe:
    result_program = update_version_program(logger)[0]
```

Ensure old YAML files with `exe: []` still proceed normally because `result_exe` becomes `True`.

- [ ] **Step 3: Compile-check touched Python files**

Run:

```bash
python -m py_compile main.py control/upgrade_exe.py
```

Expected: command exits 0.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python -m pytest tests/test_upgrade_exe.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit main wiring**

Run:

```bash
git add main.py tests/test_upgrade_exe.py
git commit -m "feat: run configured executables during upgrade"
```

## Final Verification

Run:

```bash
python -m pytest tests/test_upgrade_exe.py -v
python -m py_compile main.py control/upgrade_exe.py control/upgrade_config.py control/upgrade_db.py
git status --short
```

Expected:

- All focused tests pass.
- Compile check exits 0.
- Focused tests assert required terminal and logger output categories, not only boolean return values.
- Working tree has no unintended tracked modifications.
- Existing untracked environment folders such as `.agents/`, `.codex/`, and `__pycache__/` are not added unless already tracked by the repo.
