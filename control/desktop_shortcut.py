"""Maintain the deployed program's desktop shortcut using Windows built-ins."""

import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass


class DesktopShortcutError(Exception):
    """Windows could not inspect or create the optional desktop shortcut."""


@dataclass(frozen=True)
class DesktopShortcutResult:
    status: str
    path: str | None = None


# Paths are supplied through environment JSON, never interpolated into this code.
_SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $inputData = $env:UPGRADE_PACKAGE_SHORTCUT_INPUT | ConvertFrom-Json
    $target = [IO.Path]::GetFullPath($inputData.target)
    if ($null -eq $inputData.desktops) {
        $desktops = @(
            [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory),
            [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonDesktopDirectory)
        )
    } else {
        $desktops = @($inputData.desktops)
    }
    if ($desktops.Count -eq 0 -or [string]::IsNullOrWhiteSpace($desktops[0]) -or
        -not [IO.Directory]::Exists($desktops[0])) {
        throw 'Current user desktop directory is unavailable'
    }
    $shell = New-Object -ComObject WScript.Shell
    foreach ($desktop in ($desktops | Select-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace($desktop) -or -not [IO.Directory]::Exists($desktop)) {
            continue
        }
        foreach ($link in (Get-ChildItem -LiteralPath $desktop -Filter '*.lnk' -File -Force)) {
            try {
                $shortcut = $shell.CreateShortcut($link.FullName)
                if ([string]::IsNullOrWhiteSpace($shortcut.TargetPath)) { continue }
                $linkTarget = [IO.Path]::GetFullPath(
                    [Environment]::ExpandEnvironmentVariables($shortcut.TargetPath))
            } catch {
                # Corrupt, inaccessible, and non-file shortcuts are unrelated entries.
                continue
            }
            if ([string]::Equals($linkTarget, $target, [StringComparison]::OrdinalIgnoreCase)) {
                @{status='existing'; path=$link.FullName} | ConvertTo-Json -Compress
                exit 0
            }
        }
    }
    $number = 1
    while ($true) {
        $name = if ($number -eq 1) { 'xiting.lnk' } else { "xiting ($number).lnk" }
        $path = [IO.Path]::Combine($desktops[0], $name)
        if (Test-Path -LiteralPath $path) { $number++; continue }
        try {
            # Reserve the name exclusively so an existing object is never overwritten.
            $reservation = [IO.File]::Open($path, [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write, [IO.FileShare]::None)
            $reservation.Dispose()
            break
        } catch [IO.IOException] {
            if (Test-Path -LiteralPath $path) { $number++; continue }
            throw
        }
    }
    try {
        $shortcut = $shell.CreateShortcut($path)
        $shortcut.TargetPath = $target
        $shortcut.WorkingDirectory = [IO.Path]::GetDirectoryName($target)
        $shortcut.IconLocation = $target + ',0'
        $shortcut.Save()
    } catch {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        throw
    }
    @{status='created'; path=$path} | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""


def _powershell_path():
    return os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )


def _ensure_windows_shortcut(target, desktops=None):
    """Run the Windows operation; desktop overrides are only used by integration tests."""
    payload = {
        "target": os.path.abspath(os.fspath(target)),
        "desktops": [os.fspath(path) for path in desktops] if desktops is not None else None,
    }
    environment = os.environ.copy()
    environment["UPGRADE_PACKAGE_SHORTCUT_INPUT"] = json.dumps(payload, ensure_ascii=False)
    encoded_script = base64.b64encode(_SHORTCUT_SCRIPT.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            [_powershell_path(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded_script],
            env=environment, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30, creationflags=0x08000000,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DesktopShortcutError(f"Cannot run Windows shortcut maintenance: {error}") from error
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DesktopShortcutError(f"Windows shortcut maintenance failed: {detail}")
    try:
        result = json.loads(completed.stdout.lstrip("\ufeff"))
        if result["status"] not in ("created", "existing") or not isinstance(result["path"], str):
            raise ValueError("unexpected shortcut result")
        return DesktopShortcutResult(result["status"], result["path"])
    except (ValueError, KeyError, TypeError) as error:
        raise DesktopShortcutError("Windows shortcut maintenance returned an invalid result") from error


def ensure_desktop_shortcut(target):
    """Reuse a matching personal/public desktop link, or create a personal link."""
    if sys.platform != "win32":
        return DesktopShortcutResult("skipped")
    return _ensure_windows_shortcut(target)
