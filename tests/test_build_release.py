import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from consts.version_config import load_version_config


REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_ROOT = REPO_ROOT / "dist"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_release.ps1"
VALID_VERSION_YAML = (
    'versions: ["0.12", "10", "2"]\n'
    'target_version: "2"\n'
)


@pytest.fixture
def release_test_root():
    root = DIST_ROOT / f"test-release-{uuid.uuid4()}"
    root.mkdir(parents=True)
    try:
        yield root.resolve()
    finally:
        shutil.rmtree(root.resolve(), ignore_errors=True)


def write_resources(root):
    resources = root / "source-resources"
    marker = resources / "marker" / "content.txt"
    marker.parent.mkdir(parents=True)
    (resources / "version.yml").write_text(VALID_VERSION_YAML, encoding="utf-8")
    marker.write_text("external resource marker", encoding="utf-8")
    return resources


def run_build_script(*arguments, env=None):
    command = [
        "powershell",
        "-NoProfile",
        "-File",
        str(BUILD_SCRIPT),
        *map(str, arguments),
    ]
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_skip_build_stages_executable_and_external_resources(release_test_root):
    executable = release_test_root / "fake-main.exe"
    executable_bytes = b"fake executable marker\x00\xff"
    executable.write_bytes(executable_bytes)
    resources = write_resources(release_test_root)
    stage = release_test_root / "skip-build-stage"

    result = run_build_script(
        "-SkipBuild",
        "-ExecutableSource",
        executable,
        "-ResourcesSource",
        resources,
        "-StageDirectory",
        stage,
        "-PythonExecutable",
        sys.executable,
    )

    assert result.returncode == 0, result.stderr
    assert (stage / "main.exe").read_bytes() == executable_bytes
    assert (stage / "resources" / "version.yml").read_text(encoding="utf-8") == VALID_VERSION_YAML
    assert (stage / "resources" / "marker" / "content.txt").read_text(encoding="utf-8") == "external resource marker"
    assert load_version_config(stage / "resources" / "version.yml") == (["0.12", "10", "2"], "2")
    assert str(stage.resolve()) in result.stdout


def test_non_skip_build_invokes_exact_pyinstaller_command_and_frozen_lookup(release_test_root):
    resources = write_resources(release_test_root)
    stage = release_test_root / "built-stage"
    fake_pyinstaller = release_test_root / "pyinstaller.ps1"
    generated_executable = release_test_root / "generated-main.exe"
    recorded_arguments = release_test_root / "pyinstaller-arguments.json"
    recorded_cwd = release_test_root / "pyinstaller-cwd.txt"
    fake_pyinstaller.write_text(
        """param([Parameter(ValueFromRemainingArguments=$true)][string[]]$ReceivedArguments)
$ReceivedArguments | ConvertTo-Json -Compress | Set-Content -LiteralPath $env:FAKE_PYINSTALLER_ARGUMENTS -Encoding UTF8
(Get-Location).Path | Set-Content -LiteralPath $env:FAKE_PYINSTALLER_CWD -Encoding UTF8
[System.IO.File]::WriteAllBytes($env:FAKE_PYINSTALLER_EXECUTABLE, [byte[]](70, 65, 75, 69))
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.update(
        {
            "FAKE_PYINSTALLER_ARGUMENTS": str(recorded_arguments),
            "FAKE_PYINSTALLER_CWD": str(recorded_cwd),
            "FAKE_PYINSTALLER_EXECUTABLE": str(generated_executable),
        }
    )

    result = run_build_script(
        "-PyInstallerExecutable",
        fake_pyinstaller,
        "-ExecutableSource",
        generated_executable,
        "-ResourcesSource",
        resources,
        "-StageDirectory",
        stage,
        "-PythonExecutable",
        sys.executable,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(recorded_arguments.read_text(encoding="utf-8-sig")) == ["-F", "main.py"]
    assert Path(recorded_cwd.read_text(encoding="utf-8-sig").strip()).resolve() == REPO_ROOT
    assert (stage / "main.exe").read_bytes() == b"FAKE"

    frozen_check = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys; "
                "sys.frozen = True; "
                "sys.executable = sys.argv[1]; "
                "import consts.program_config as config; "
                "print(json.dumps({"
                "'path': str(config.VERSION_CONFIG_PATH.resolve()), "
                "'versions': config.ALL_VERSIONS, "
                "'target': config.TARGET_VERSION}))"
            ),
            str(stage / "main.exe"),
        ],
        cwd=release_test_root,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert frozen_check.returncode == 0, frozen_check.stderr
    frozen_config = json.loads(frozen_check.stdout)
    expected_versions, expected_target = load_version_config(stage / "resources" / "version.yml")
    assert Path(frozen_config["path"]) == (stage / "resources" / "version.yml").resolve()
    assert frozen_config["versions"] == expected_versions
    assert frozen_config["target"] == expected_target


def test_missing_source_version_file_fails_before_staging(release_test_root):
    resources = release_test_root / "source-resources"
    resources.mkdir()
    stage = release_test_root / "missing-config-stage"
    fake_pyinstaller = release_test_root / "fail-if-invoked.ps1"
    build_marker = release_test_root / "build-was-invoked.txt"
    fake_pyinstaller.write_text(
        "Set-Content -LiteralPath $env:BUILD_MARKER -Value invoked\n",
        encoding="utf-8",
    )
    env = {**os.environ, "BUILD_MARKER": str(build_marker)}

    result = run_build_script(
        "-PyInstallerExecutable",
        fake_pyinstaller,
        "-ExecutableSource",
        release_test_root / "generated-main.exe",
        "-ResourcesSource",
        resources,
        "-StageDirectory",
        stage,
        "-PythonExecutable",
        sys.executable,
        env=env,
    )

    assert result.returncode != 0
    assert "version.yml" in (result.stdout + result.stderr)
    assert not build_marker.exists()
    assert not stage.exists()


def test_missing_executable_fails_before_staging(release_test_root):
    resources = write_resources(release_test_root)
    stage = release_test_root / "missing-executable-stage"

    result = run_build_script(
        "-SkipBuild",
        "-ExecutableSource",
        release_test_root / "missing-main.exe",
        "-ResourcesSource",
        resources,
        "-StageDirectory",
        stage,
        "-PythonExecutable",
        sys.executable,
    )

    assert result.returncode != 0
    assert "executable" in (result.stdout + result.stderr).lower()
    assert not stage.exists()


@pytest.mark.parametrize("unsafe_stage", [DIST_ROOT, REPO_ROOT])
def test_stage_must_be_strict_descendant_of_repository_dist(release_test_root, unsafe_stage):
    executable = release_test_root / "fake-main.exe"
    executable.write_bytes(b"fake")
    resources = write_resources(release_test_root)
    sentinel = release_test_root / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")

    result = run_build_script(
        "-SkipBuild",
        "-ExecutableSource",
        executable,
        "-ResourcesSource",
        resources,
        "-StageDirectory",
        unsafe_stage,
        "-PythonExecutable",
        sys.executable,
    )

    assert result.returncode != 0
    assert "dist" in (result.stdout + result.stderr).lower()
    assert sentinel.read_text(encoding="utf-8") == "untouched"
