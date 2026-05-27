import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

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


@pytest.fixture
def local_tmp_path():
    path = Path(__file__).parent / ".tmp" / uuid.uuid4().hex
    base_dir = path / "base"
    base_dir.mkdir(parents=True)
    try:
        yield base_dir
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write_script(path, body):
    path.write_text(body, encoding="utf-8")
    return path


def write_version_file(base_dir, data):
    version_file = base_dir / "resources" / "version_1.0.yml"
    version_file.parent.mkdir(exist_ok=True)
    version_file.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return version_file


def logged_text(logger):
    return "\n".join(message for _level, message in logger.messages)


def test_empty_upgrade_versions_returns_success(local_tmp_path):
    manager = ExeUpgradeManager(DummyLogger(), [], base_dir=str(local_tmp_path))

    assert manager.run() is True


def test_missing_exe_section_returns_success(local_tmp_path):
    version_file = local_tmp_path / "resources" / "version_1.0.yml"
    version_file.parent.mkdir()
    version_file.write_text("version: '1.0'\nconfig: []\n", encoding="utf-8")

    manager = ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path))

    assert manager.run() is True


def test_empty_and_null_exe_sections_return_success(local_tmp_path):
    write_version_file(local_tmp_path, {"version": "1.0", "exe": []})
    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is True

    write_version_file(local_tmp_path, {"version": "1.0", "exe": None})
    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is True


@pytest.mark.parametrize("exe_value", ["", 0, {}])
def test_falsey_malformed_top_level_exe_values_fail(local_tmp_path, exe_value):
    logger = DummyLogger()
    write_version_file(local_tmp_path, {"version": "1.0", "exe": exe_value})

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(local_tmp_path)).run() is False
    assert "exe 配置必须是列表" in logged_text(logger)


def test_required_command_success_returns_success(local_tmp_path):
    script = write_script(local_tmp_path / "ok.py", "print('hello exe')\n")
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"path": sys.executable, "args": [str(script)]}]},
    )

    manager = ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path))

    assert manager.run() is True


def test_required_nonzero_command_fails(local_tmp_path):
    script = write_script(local_tmp_path / "fail.py", "import sys\nsys.exit(7)\n")
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"path": sys.executable, "args": [str(script)]}]},
    )

    manager = ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path))

    assert manager.run() is False


def test_optional_nonzero_command_does_not_fail(local_tmp_path):
    script = write_script(local_tmp_path / "fail.py", "import sys\nsys.exit(7)\n")
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"path": sys.executable, "args": [str(script)], "required": False}]},
    )

    manager = ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path))

    assert manager.run() is True


def test_missing_required_path_fails(local_tmp_path):
    write_version_file(local_tmp_path, {"version": "1.0", "exe": [{"args": ["value"]}]})

    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is False


def test_missing_optional_path_is_logged_and_skipped(local_tmp_path):
    logger = DummyLogger()
    write_version_file(local_tmp_path, {"version": "1.0", "exe": [{"required": False}]})

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(local_tmp_path)).run() is True
    assert "exe 配置 path 必须是非空字符串" in logged_text(logger)


def test_plain_string_args_fails(local_tmp_path):
    write_version_file(local_tmp_path, {"version": "1.0", "exe": [{"path": sys.executable, "args": "-V"}]})

    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is False


def test_relative_path_that_escapes_base_fails(local_tmp_path):
    outside = local_tmp_path.parent / "outside.exe"
    outside.write_text("not used", encoding="utf-8")
    write_version_file(local_tmp_path, {"version": "1.0", "exe": [{"path": "../outside.exe"}]})

    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is False


def test_nonexistent_explicit_cwd_fails(local_tmp_path):
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"path": sys.executable, "cwd": str(local_tmp_path / "missing")}]},
    )

    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is False


def test_explicit_cwd_is_used(local_tmp_path, capsys):
    work_dir = local_tmp_path / "work"
    work_dir.mkdir()
    script = write_script(local_tmp_path / "cwd.py", "import os\nprint(os.getcwd())\n")
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"path": sys.executable, "args": [str(script)], "cwd": str(work_dir)}]},
    )

    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is True

    assert str(work_dir) in capsys.readouterr().out


def test_positive_timeout_is_honored(local_tmp_path):
    script = write_script(local_tmp_path / "quick.py", "print('quick')\n")
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"path": sys.executable, "args": [str(script)], "timeout": 5}]},
    )

    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is True


def test_required_timeout_failure_returns_false(local_tmp_path):
    script = write_script(local_tmp_path / "slow.py", "import time\ntime.sleep(1)\n")
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"path": sys.executable, "args": [str(script)], "timeout": 0.01}]},
    )

    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is False


def test_optional_timeout_failure_returns_true_and_logs(local_tmp_path, capsys):
    logger = DummyLogger()
    script = write_script(local_tmp_path / "slow.py", "import time\ntime.sleep(1)\n")
    write_version_file(
        local_tmp_path,
        {
            "version": "1.0",
            "exe": [{"name": "SlowTool", "path": sys.executable, "args": [str(script)], "timeout": 0.01, "required": False}],
        },
    )

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(local_tmp_path)).run() is True

    printed = capsys.readouterr().out
    logs = logged_text(logger)
    assert "可执行程序执行超时" in printed
    assert "SlowTool" in printed
    assert "0.01" in printed
    assert "可执行程序执行超时" in logs


def test_string_shorthand_launches_with_argument_list_and_shell_false(local_tmp_path, monkeypatch):
    calls = []
    write_version_file(local_tmp_path, {"version": "1.0", "exe": [sys.executable]})

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("control.upgrade_exe.subprocess.run", fake_run)

    assert ExeUpgradeManager(DummyLogger(), ["1.0"], base_dir=str(local_tmp_path)).run() is True
    command, kwargs = calls[0]
    assert command == [sys.executable]
    assert kwargs["shell"] is False


def test_success_prints_and_logs_start_end_stdout_and_stderr(local_tmp_path, capsys):
    script = write_script(
        local_tmp_path / "talk.py",
        "import sys\nprint('out text')\nprint('err text', file=sys.stderr)\n",
    )
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"name": "Talker", "path": sys.executable, "args": [str(script)]}]},
    )
    logger = DummyLogger()

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(local_tmp_path)).run() is True

    printed = capsys.readouterr().out
    logs = logged_text(logger)
    for text in ["启动可执行程序", "可执行程序执行结束", "out text", "err text"]:
        assert text in printed
        assert text in logs


def test_undecodable_process_output_is_decoded_with_replacement(local_tmp_path, monkeypatch, capsys):
    calls = []
    write_version_file(local_tmp_path, {"version": "1.0", "exe": [{"name": "ByteTool", "path": sys.executable}]})
    logger = DummyLogger()

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"bad\xffout", stderr=b"bad\xfeerr")

    monkeypatch.setattr("control.upgrade_exe.subprocess.run", fake_run)

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(local_tmp_path)).run() is True

    printed = capsys.readouterr().out
    logs = logged_text(logger)
    assert calls[0][1]["text"] is False
    for text in ["bad\ufffdout", "bad\ufffderr"]:
        assert text in printed
        assert text in logs


def test_nonzero_output_includes_display_name_and_exit_code(local_tmp_path, capsys):
    script = write_script(local_tmp_path / "fail.py", "import sys\nsys.exit(7)\n")
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"name": "FailTool", "path": sys.executable, "args": [str(script)]}]},
    )
    logger = DummyLogger()

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(local_tmp_path)).run() is False

    printed = capsys.readouterr().out
    logs = logged_text(logger)
    for text in ["可执行程序返回非零退出码", "FailTool", "7"]:
        assert text in printed
        assert text in logs


def test_timeout_output_includes_display_name_and_timeout(local_tmp_path, capsys):
    script = write_script(local_tmp_path / "slow.py", "import time\ntime.sleep(1)\n")
    write_version_file(
        local_tmp_path,
        {"version": "1.0", "exe": [{"name": "SlowTool", "path": sys.executable, "args": [str(script)], "timeout": 0.01}]},
    )
    logger = DummyLogger()

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(local_tmp_path)).run() is False

    printed = capsys.readouterr().out
    logs = logged_text(logger)
    for text in ["可执行程序执行超时", "SlowTool", "0.01"]:
        assert text in printed
        assert text in logs


@pytest.mark.parametrize(
    ("stdout", "stderr", "required", "expected_result"),
    [
        (b"partial out bytes\n", b"partial err bytes\n", True, False),
        ("partial out text\n", "partial err text\n", False, True),
    ],
)
def test_timeout_prints_and_logs_captured_stdout_and_stderr(
    local_tmp_path,
    monkeypatch,
    capsys,
    stdout,
    stderr,
    required,
    expected_result,
):
    write_version_file(
        local_tmp_path,
        {
            "version": "1.0",
            "exe": [
                {
                    "name": "SlowTool",
                    "path": sys.executable,
                    "timeout": 1,
                    "required": required,
                }
            ],
        },
    )
    logger = DummyLogger()

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=stdout, stderr=stderr)

    monkeypatch.setattr("control.upgrade_exe.subprocess.run", fake_run)

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(local_tmp_path)).run() is expected_result

    printed = capsys.readouterr().out
    logs = logged_text(logger)
    for text in ["可执行程序执行超时", "SlowTool", "partial out", "partial err"]:
        assert text in printed
        assert text in logs


def test_start_failure_output_includes_display_name_path_and_exception(local_tmp_path, monkeypatch, capsys):
    exe_file = local_tmp_path / "tool.exe"
    exe_file.write_text("not actually launched", encoding="utf-8")
    write_version_file(local_tmp_path, {"version": "1.0", "exe": [{"name": "BrokenTool", "path": str(exe_file)}]})
    logger = DummyLogger()

    def fake_run(command, **kwargs):
        raise OSError("fake start failure")

    monkeypatch.setattr("control.upgrade_exe.subprocess.run", fake_run)

    assert ExeUpgradeManager(logger, ["1.0"], base_dir=str(local_tmp_path)).run() is False

    printed = capsys.readouterr().out
    logs = logged_text(logger)
    for text in ["可执行程序启动失败", "BrokenTool", str(exe_file), "fake start failure"]:
        assert text in printed
        assert text in logs
