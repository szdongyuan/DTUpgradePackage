# EXE Launch During Upgrade Design

## Purpose

Add support for version YAML files to declare executable programs that should be launched during an upgrade. The upgrader must wait for each declared program to finish, print start/end/error information to the terminal, and log the same operational messages.

## Current Context

The upgrade entry point is `main.py`. It currently performs database backup, database upgrade, config upgrade, main program copy, and then either commits the new version information or restores database/config backups on failure.

Version YAML files live in `resources/version_<version>.yml`. Existing files include top-level sections such as `db`, `ai`, `ui`, `config`, and `exe`. The active example `resources/version_0.25.07.01.yml` already has `exe: []`, but no code currently reads or executes this section.

## Supported YAML Format

The new `exe` section is optional. Missing `exe`, `exe: []`, or `exe: null` means no executable programs are launched.

Preferred new format:

```yaml
exe:
  - name: "Example Tool"
    path: "resources/tools/example.exe"
    args: ["--mode", "upgrade"]
    cwd: "resources/tools"
    timeout: 600
    required: true
```

Field behavior:

- `name`: Optional display name. If omitted, use `path`.
- `path`: Required for each executable item. Relative paths are resolved from `DEFAULT_DIR`, the upgrade package directory.
- `args`: Optional command-line arguments. Accept a list of strings or numbers. Missing/null means no arguments.
- `cwd`: Optional working directory. Relative paths are resolved from `DEFAULT_DIR`. Missing/null means use the executable file's parent directory.
- `timeout`: Optional maximum runtime in seconds. Missing/null means wait indefinitely.
- `required`: Optional boolean. Defaults to `true`.

Field validation:

- `path` must be a non-empty string. Missing, null, empty, or non-string `path` is a malformed entry.
- `name`, when provided, must be a non-empty string. Invalid `name` does not fail the entry; the display name falls back to `path`.
- `args`, when provided, must be a list. Each list item must be a string, integer, or float and is converted to a string before launch. A plain string is not accepted as `args` because it is ambiguous; use `args: ["value"]`.
- `cwd`, when provided, must be a non-empty string resolving to an existing directory.
- `timeout`, when provided, must be a positive integer or float greater than zero.
- `required`, when provided, must be a boolean. Invalid `required` falls back to the default `true`.
- Malformed entries or invalid launch fields fail the upgrade when the effective `required` value is `true`; when the effective `required` value is `false`, they are printed/logged and skipped.

Compatibility fallback:

- A string item inside `exe` is accepted as a shorthand for `{"path": "<string>"}`.
- Dict entries with invalid fields use normal field defaults where possible.
- Non-dict and non-string entries are treated as malformed required entries because they cannot declare `required: false`; they must fail the upgrade.

## Execution Flow

`main.py` will add an executable launch step after database and config upgrades have succeeded and before copying `resources/DiTing.exe` to the target program directory.

The high-level flow becomes:

1. Back up the database.
2. Determine current version and build managers.
3. Run database upgrade.
4. Run config upgrade.
5. Run YAML-declared executables for the upgrade versions.
6. Copy the main program executable.
7. On full success, update version metadata and remove backups.
8. On failure, restore database/config backups through the existing failure path.

The executable runner must process version YAML files in the same version order and with the same version-selection algorithm as config upgrades. To avoid drift, `main.py` should pass `config_manager.upgrade_version_list` into the executable runner instead of recomputing the list independently. If a future refactor hides that attribute, the shared algorithm must still be extracted rather than duplicated.

## Terminal And Log Output

For every executable item, print and log:

- Start message including display name, resolved path, arguments, and working directory.
- End message including display name and exit code when the process starts and returns normally.
- Captured stdout when present.
- Captured stderr when present.
- Start failure message including display name, resolved path, and exception text when the process cannot be started.
- Timeout message including display name and timeout seconds when the process exceeds `timeout`.
- Non-zero exit message including display name and exit code when the process returns a failing code.

Messages should be plain terminal text consistent with the existing Chinese user-facing output style in `main.py` and `control/upgrade_config.py`.

## Error Handling

The executable runner returns a boolean success result to `main.py`.

Required executable failures return `False` and cause the existing upgrade failure branch to run. Failures include:

- Missing/invalid `path`.
- Invalid `args`, `cwd`, or `timeout`.
- Executable path not found.
- Process start exception.
- Timeout.
- Non-zero exit code.

Optional executable failures (`required: false`) are printed and logged as errors or warnings, but do not make the runner return `False`.

If multiple executable entries are declared, they run sequentially in YAML order. The runner stops immediately on the first required failure.

Executable side effects are outside the upgrader's rollback responsibility. Declared executables must be idempotent or perform their own cleanup if they modify files, databases, services, or external state. On required executable failure, the upgrader only performs its existing rollback: database restore from the backup made at startup and config restore through `ConfigManager.restore_config()`.

## Implementation Shape

Create a small focused manager in `control/upgrade_exe.py` rather than expanding `main.py`.

Responsibilities:

- Accept the already-computed upgrade version list from `ConfigManager` so executable processing uses the same versions as config processing.
- Load each version YAML using the existing `Readyml.load_yaml` helper to follow current repository patterns.
- Normalize `exe` entries into a predictable internal representation.
- Resolve relative paths safely using `DEFAULT_DIR`.
- Launch executables with `subprocess.run`.
- Print and log lifecycle messages.
- Return `True` or `False` to the main upgrade flow.

`main.py` should import this manager, invoke it after config upgrade success, and include its result in the condition that gates main program replacement.

Path resolution rules:

- Relative `path` and `cwd` values are normalized against `DEFAULT_DIR`.
- Relative paths must remain inside `DEFAULT_DIR` after real path resolution with `os.path.realpath`. Relative paths that escape with `..` or symlinks are malformed.
- Absolute `path` and `cwd` values are allowed because an upgrade may need to launch an already-installed helper. They must still be passed to `subprocess.run` as argument lists, never through `shell=True`.
- Resolved executable `path` must exist and be a file before launch.
- Resolved `cwd`, whether explicit or defaulted, must exist and be a directory before launch.
- If `cwd` is omitted and the executable `path` is relative, the default working directory is the resolved executable parent directory.
- If `cwd` is omitted and the executable `path` is absolute, the default working directory is the executable parent directory.
- Invalid `cwd` is treated as an invalid launch field before calling `subprocess.run`, not left to a later process start exception.

## Testing

Add focused tests for the executable manager where practical:

- Empty/missing `exe` returns success and launches nothing.
- String shorthand normalizes to a path-only required executable.
- Dict entries honor `args`, `cwd`, `timeout`, and `required`.
- Required non-zero process result fails.
- Optional non-zero process result does not fail.
- Missing required path fails.

Use temporary files and a lightweight Python command as the launched process so tests do not depend on real `.exe` files. If the existing repository has no test harness, add minimal `pytest` coverage for the new manager without broad refactors.

## Compatibility / Migration

Backward compatibility: required

Protected surfaces:

- Existing version YAML files with no `exe`, empty `exe`, or unrelated top-level sections.
- Existing database upgrade behavior.
- Existing config upgrade behavior.
- Existing success/failure terminal flow and backup restore behavior.

Allowed breakage:

- None for existing YAML files.
- New malformed required `exe` entries may fail the upgrade because they represent an explicit new instruction.

Migration strategy: compatibility layer

The compatibility layer is the tolerant parser for `exe`: missing/null/empty means no-op, and string entries are accepted as shorthand. Existing YAML files do not require edits.

## Out Of Scope

- Running executables in parallel.
- Streaming output live while the process is still running.
- Killing process trees beyond the direct timeout behavior provided by `subprocess.run`.
- Adding UI controls for executable launch configuration.
- Changing database or config upgrade semantics beyond inserting the new executable step.
