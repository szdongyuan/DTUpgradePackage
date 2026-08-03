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


def run_build_script(*arguments, env=None, script=BUILD_SCRIPT):
    command = [
        "powershell",
        "-NoProfile",
        "-File",
        str(script),
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


def create_directory_reparse_point(link, target):
    assert link.is_absolute()
    assert target.is_absolute()
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (NotImplementedError, OSError):
        if os.path.lexists(link):
            remove_directory_reparse_point(link)

    junction_result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction_result.returncode != 0:
        pytest.skip(
            "Windows directory symlink and junction creation are unavailable: "
            + junction_result.stderr
        )


def remove_directory_reparse_point(link):
    if not os.path.lexists(link):
        return
    if link.is_symlink():
        link.unlink()
        return
    if getattr(os.path, "isjunction", lambda _path: False)(link):
        os.rmdir(link)
        return
    raise AssertionError(f"Refusing to remove non-reparse directory: {link}")


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


def test_nonzero_pyinstaller_exit_fails_without_staging(release_test_root):
    resources = write_resources(release_test_root)
    stage = release_test_root / "failed-build-stage"
    fake_pyinstaller = release_test_root / "pyinstaller-fails.cmd"
    fake_pyinstaller.write_text("@echo off\r\nexit /b 23\r\n", encoding="utf-8")

    result = run_build_script(
        "-PyInstallerExecutable",
        fake_pyinstaller,
        "-ExecutableSource",
        release_test_root / "never-created.exe",
        "-ResourcesSource",
        resources,
        "-StageDirectory",
        stage,
        "-PythonExecutable",
        sys.executable,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "PyInstaller failed with exit code 23" in output
    assert not stage.exists()


def test_successful_pyinstaller_without_executable_fails_without_staging(release_test_root):
    resources = write_resources(release_test_root)
    stage = release_test_root / "missing-build-output-stage"
    fake_pyinstaller = release_test_root / "pyinstaller-no-output.cmd"
    fake_pyinstaller.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    executable = release_test_root / "not-generated.exe"

    result = run_build_script(
        "-PyInstallerExecutable",
        fake_pyinstaller,
        "-ExecutableSource",
        executable,
        "-ResourcesSource",
        resources,
        "-StageDirectory",
        stage,
        "-PythonExecutable",
        sys.executable,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "PyInstaller completed but executable source is missing:" in output
    assert executable.name in output
    assert not stage.exists()


@pytest.mark.parametrize(
    ("version_yaml", "reason"),
    [
        ("versions: [\n", "invalid YAML"),
        ('versions: ["1"]\ntarget_version: "1"\n', "versions must contain 0.12"),
    ],
)
def test_invalid_staged_version_config_fails_without_release_artifact(
    release_test_root, version_yaml, reason
):
    executable = release_test_root / "fake-main.exe"
    executable.write_bytes(b"fake")
    resources = write_resources(release_test_root)
    (resources / "version.yml").write_text(version_yaml, encoding="utf-8")
    stage = release_test_root / "invalid-config-stage"

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

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert reason in output
    assert "Staged version configuration validation failed" in output
    assert "Staged release:" not in output
    assert not stage.exists()


def test_validator_launch_failure_removes_partial_stage(release_test_root):
    executable = release_test_root / "fake-main.exe"
    executable.write_bytes(b"fake")
    resources = write_resources(release_test_root)
    stage = release_test_root / "validator-launch-failure-stage"
    missing_python = release_test_root / "missing-python.exe"

    result = run_build_script(
        "-SkipBuild",
        "-ExecutableSource",
        executable,
        "-ResourcesSource",
        resources,
        "-StageDirectory",
        stage,
        "-PythonExecutable",
        missing_python,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert missing_python.name in output
    assert "not recognized" in output
    assert "Staged release:" not in output
    assert not stage.exists()


def test_repository_dist_reparse_point_is_rejected_without_touching_target(release_test_root):
    fake_repository = release_test_root / "fake-repository"
    copied_script = fake_repository / "scripts" / "build_release.ps1"
    copied_script.parent.mkdir(parents=True)
    shutil.copy2(BUILD_SCRIPT, copied_script)

    external_dist = release_test_root / "external-dist-target"
    external_dist.mkdir()
    sentinel = external_dist / "sentinel.txt"
    sentinel.write_text("preserve me", encoding="utf-8")
    dist_link = fake_repository / "dist"
    create_directory_reparse_point(dist_link, external_dist.resolve())

    executable = release_test_root / "fake-main.exe"
    executable.write_bytes(b"fake")
    resources = write_resources(release_test_root)
    try:
        result = run_build_script(
            "-SkipBuild",
            "-ExecutableSource",
            executable,
            "-ResourcesSource",
            resources,
            "-PythonExecutable",
            sys.executable,
            script=copied_script,
        )

        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "repository dist directory must not be a symbolic link or junction" in output.lower()
        assert sentinel.read_text(encoding="utf-8") == "preserve me"
        assert not (external_dist / "UpgradePackage").exists()
    finally:
        remove_directory_reparse_point(dist_link)


def test_stage_descendant_reparse_point_is_rejected_without_touching_target(release_test_root):
    executable = (release_test_root / "fake-main.exe").resolve()
    executable.write_bytes(b"fake")
    resources = write_resources(release_test_root).resolve()

    fake_repository = release_test_root / "fake-component-repository"
    copied_script = fake_repository / "scripts" / "build_release.ps1"
    copied_script.parent.mkdir(parents=True)
    shutil.copy2(BUILD_SCRIPT, copied_script)
    fake_dist = (fake_repository / "dist").resolve()
    fake_dist.mkdir()

    external_target = (release_test_root / "external-stage-target").resolve()
    assert external_target.parent == release_test_root
    assert not external_target.is_relative_to(fake_dist)
    (external_target / "existing").mkdir(parents=True)
    (external_target / "sentinel.txt").write_bytes(b"preserve sentinel exactly\x00\xff")
    (external_target / "existing" / "content.bin").write_bytes(b"existing target content")
    before_files = {
        path.relative_to(external_target): path.read_bytes()
        for path in external_target.rglob("*")
        if path.is_file()
    }
    before_directories = {
        path.relative_to(external_target)
        for path in external_target.rglob("*")
        if path.is_dir()
    }

    stage_link = fake_dist / "linked-stage-component"
    try:
        create_directory_reparse_point(stage_link, external_target)
        # Keep the absolute lexical path under the fake repository dist. Resolving it after
        # link creation would follow the link and bypass the component-specific guard.
        stage = stage_link / "nested-stage"
        result = run_build_script(
            "-SkipBuild",
            "-ExecutableSource",
            executable,
            "-ResourcesSource",
            resources,
            "-StageDirectory",
            stage,
            "-PythonExecutable",
            Path(sys.executable).resolve(),
            script=copied_script,
        )

        output = result.stdout + result.stderr
        assert result.returncode != 0
        assert "Stage directory must not traverse a symbolic link or junction:" in output
        assert "Staged release:" not in output
        after_files = {
            path.relative_to(external_target): path.read_bytes()
            for path in external_target.rglob("*")
            if path.is_file()
        }
        after_directories = {
            path.relative_to(external_target)
            for path in external_target.rglob("*")
            if path.is_dir()
        }
        assert after_files == before_files
        assert after_directories == before_directories
        assert not (external_target / "nested-stage").exists()
    finally:
        remove_directory_reparse_point(stage_link)


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
