import base64
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from control import desktop_shortcut


def test_non_windows_skips_without_starting_powershell(monkeypatch):
    monkeypatch.setattr(desktop_shortcut.sys, "platform", "linux")
    monkeypatch.setattr(
        desktop_shortcut.subprocess, "run",
        lambda *_args, **_kwargs: pytest.fail("PowerShell must not run"),
    )
    assert desktop_shortcut.ensure_desktop_shortcut("xiting.exe").status == "skipped"


def test_paths_are_data_and_powershell_is_hidden_and_bounded(monkeypatch, tmp_path):
    target = tmp_path / "中文 ' & $() [test]" / "xiting.exe"
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "created", "path": "fake-desktop/xiting.lnk"}),
            stderr="",
        )

    monkeypatch.setattr(desktop_shortcut.sys, "platform", "win32")
    monkeypatch.setattr(desktop_shortcut.subprocess, "run", fake_run)
    result = desktop_shortcut.ensure_desktop_shortcut(target)
    assert result.status == "created"
    assert result.path == "fake-desktop/xiting.lnk"
    args, kwargs = calls[0]
    assert args[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    script = base64.b64decode(args[4]).decode("utf-16-le")
    assert str(target) not in script
    assert json.loads(kwargs["env"]["UPGRADE_PACKAGE_SHORTCUT_INPUT"]) == {
        "target": os.path.abspath(target),
        "desktops": None,
    }
    assert kwargs["creationflags"] & 0x08000000
    assert 0 < kwargs["timeout"] <= 30
    assert kwargs.get("shell", False) is False


@pytest.mark.parametrize("failure", ["exit", "timeout", "invalid-result"])
def test_powershell_failures_raise_actionable_error(monkeypatch, failure):
    def fake_run(*_args, **_kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("powershell.exe", 30)
        return SimpleNamespace(
            returncode=1 if failure == "exit" else 0,
            stdout="invalid result",
            stderr="desktop access denied" if failure == "exit" else "",
        )

    monkeypatch.setattr(desktop_shortcut.sys, "platform", "win32")
    monkeypatch.setattr(desktop_shortcut.subprocess, "run", fake_run)
    with pytest.raises(desktop_shortcut.DesktopShortcutError):
        desktop_shortcut.ensure_desktop_shortcut("xiting.exe")


@pytest.fixture
def fake_desktops(tmp_path):
    # Every integration call overrides both Windows desktop locations.
    personal = tmp_path / "redirected 中文 ' & $() [desktop]"
    public = tmp_path / "public desktop"
    personal.mkdir()
    public.mkdir()
    target = tmp_path / "program 中文 ' & $() [target]" / "xiting.exe"
    target.parent.mkdir()
    target.write_bytes(b"not an executable; must never run")
    return target, personal, public


def _read_link(link):
    script = """
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:UPGRADE_TEST_LINK)
@{target=$s.TargetPath; working=$s.WorkingDirectory; icon=$s.IconLocation} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [desktop_shortcut._powershell_path(), "-NoProfile", "-NonInteractive",
         "-EncodedCommand", base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
        env={**os.environ, "UPGRADE_TEST_LINK": str(link)},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=0x08000000,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.lstrip("\ufeff"))


windows_only = pytest.mark.skipif(os.name != "nt", reason="real Windows WSH integration")


@windows_only
def test_real_shortcut_creation_readback_and_repeat(fake_desktops):
    target, personal, public = fake_desktops
    first = desktop_shortcut._ensure_windows_shortcut(target, [personal, public])
    assert first.status == "created"
    assert first.path == str(personal / "xiting.lnk")
    details = _read_link(first.path)
    assert details["target"] == str(target)
    assert details["working"] == str(target.parent)
    assert details["icon"] == f"{target},0"
    before = (personal / "xiting.lnk").read_bytes()
    second = desktop_shortcut._ensure_windows_shortcut(target, [personal, public])
    assert second.status == "existing"
    assert (personal / "xiting.lnk").read_bytes() == before
    assert list(public.iterdir()) == []


@windows_only
@pytest.mark.parametrize("location", ["personal", "public"])
def test_real_renamed_matching_shortcut_is_preserved(fake_desktops, location):
    target, personal, public = fake_desktops
    result = desktop_shortcut._ensure_windows_shortcut(target, [personal, public])
    renamed = (personal if location == "personal" else public) / "用户自定义名称.LNK"
    os.replace(result.path, renamed)
    original = renamed.read_bytes()
    # Windows path spelling and redundant components must not cause duplicates.
    equivalent = str(target.parent / ".." / target.parent.name / target.name).upper()
    result = desktop_shortcut._ensure_windows_shortcut(equivalent, [personal, public])
    assert result.status == "existing"
    assert result.path == str(renamed)
    assert renamed.read_bytes() == original
    assert not (personal / "xiting.lnk").exists()


@windows_only
def test_real_unrelated_file_and_directory_collisions_are_preserved(fake_desktops):
    target, personal, public = fake_desktops
    unrelated = target.parent / "other.exe"
    unrelated.write_bytes(b"other")
    desktop_shortcut._ensure_windows_shortcut(unrelated, [personal, public])
    occupied = personal / "xiting.lnk"
    before = occupied.read_bytes()
    (personal / "xiting (2).lnk").mkdir()
    result = desktop_shortcut._ensure_windows_shortcut(target, [personal, public])
    assert result.path == str(personal / "xiting (3).lnk")
    assert occupied.read_bytes() == before
    assert (personal / "xiting (2).lnk").is_dir()
    assert _read_link(result.path)["target"] == str(target)


@windows_only
def test_real_malformed_shortcut_collision_is_preserved(fake_desktops):
    target, personal, public = fake_desktops
    occupied = personal / "xiting.lnk"
    occupied.write_bytes(b"unrelated plain-text file, not a shortcut")
    (public / "broken.lnk").write_bytes(b"another invalid link")
    result = desktop_shortcut._ensure_windows_shortcut(target, [personal, public])
    assert result.path == str(personal / "xiting (2).lnk")
    assert occupied.read_bytes() == b"unrelated plain-text file, not a shortcut"
    assert (public / "broken.lnk").read_bytes() == b"another invalid link"
    assert _read_link(result.path)["target"] == str(target)


@windows_only
def test_real_missing_desktop_reports_failure_without_creating_directory(fake_desktops):
    target, personal, public = fake_desktops
    personal.rmdir()
    with pytest.raises(desktop_shortcut.DesktopShortcutError):
        desktop_shortcut._ensure_windows_shortcut(target, [personal, public])
    assert not personal.exists()
