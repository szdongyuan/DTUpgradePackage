# External Version Configuration Design

## Summary

Move the ordered upgrade-version sequence and target version out of Python source code and into an external YAML file at `resources/version.yml`. The application will load and validate this file once during process startup, then expose the resulting values through the existing `ALL_VERSIONS` and `TARGET_VERSION` interfaces.

## Goals

- Allow release maintainers to change the supported upgrade sequence and target version without editing Python source code.
- Keep the version order explicit because it determines upgrade execution order.
- Ensure every subsystem uses one consistent configuration snapshot for the lifetime of the process.
- Fail safely and visibly when the external configuration is missing or invalid.
- Preserve the existing database-upgrade, file-upgrade, executable-upgrade, and version-persistence behavior for valid configuration.

## Non-goals

- Changing the format or meaning of `resources/version_<version>.yml` upgrade manifests.
- Automatically discovering versions from filenames in the `resources` directory.
- Changing the database schema or the initial version used when creating a legacy `system_info_table`.
- Reloading version configuration while an upgrade process is already running.
- Refactoring unrelated path or configuration handling.

## External File Format

The application reads `resources/version.yml`, relative to the same application base directory currently represented by `DEFAULT_DIR`.

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

The order of `versions` is authoritative. Version strings are identifiers and are not numerically sorted by the loader.

## Validation Rules

Loading succeeds only when all of the following are true:

- The file exists and can be read as UTF-8.
- The YAML document is a mapping.
- `versions` is a non-empty list.
- Every `versions` entry is a non-empty string.
- Every version string is already trimmed and matches the filename-safe pattern
  `[A-Za-z0-9][A-Za-z0-9._-]*`; `.` and `..` are explicitly rejected.
- Version entries are unique.
- `target_version` is a non-empty string.
- `target_version` occurs in `versions`.
- The legacy initialization version `0.12` occurs in `versions` so that creation
  of a missing legacy `system_info_table` cannot produce a version outside the
  configured sequence.

The target does not have to be the last item. Versions after the target remain configured but are not executed, matching the current upgrade boundary behavior.

Values are validated exactly as written. The loader does not trim or normalize
identifiers because that could make the configured identity differ from the
corresponding `resources/version_<version>.yml` filename.

## Upgrade Selection Semantics

All upgrade selection uses list positions, not lexical or numeric comparison:

1. Find the current version index in `versions`.
2. Find the configured target index in `versions`.
3. If the current index is before the target index, execute the slice after the
   current version through and including the target.
4. If the current index equals the target index, execute no per-version upgrade
   manifests; existing same-version orchestration behavior remains unchanged.
5. If the current index follows the target index, treat the package as older
   than the installed system and finish successfully before backup or any write.
   The newer stored current version is retained and target-version persistence
   is not invoked.

A stored current version absent from `versions` is a fatal compatibility error.
The orchestrated path detects it during a read-only preflight before backup or
any other mutation. It returns failure through a distinct preflight error path
that does not construct upgrade managers and does not invoke rollback or database
restore. Both configuration upgrades and database upgrades use the same
index-based selection semantics. In particular, `control/upgrade_config.py` will
no longer use string comparisons to decide which versions to run.

A missing legacy `system_info_table` or missing current-version row is not an
unknown stored version. The existing standalone `DataBaseManager.upgrade()`
initialization path continues to create metadata at `0.12`, which is why valid
configuration must contain `0.12`. The main orchestrated path's existing failure
behavior for unavailable current-version metadata is unchanged by this feature.

## Architecture and Data Flow

`consts/program_config.py` remains the compatibility boundary for configuration consumers. It will:

1. Establish `DEFAULT_DIR` using the existing executable-relative path behavior.
2. Load `resources/version.yml` through one small, focused loader.
3. Validate the parsed data.
4. Assign the validated values to `ALL_VERSIONS` and `TARGET_VERSION` once.

Existing consumers in `control/upgrade_config.py` and `control/upgrade_db.py` continue importing and using `ALL_VERSIONS` and `TARGET_VERSION`. This avoids changing manager constructors and keeps one consistent in-memory snapshot across the complete upgrade run.

A shared selection function will derive the target-bounded upgrade slice from
the validated sequence. Both consumers use it so their ordering and unknown
current-version behavior cannot diverge.

`main.run_upgrade()` performs a read-only current-version preflight before its
backup. When the stored current version follows the configured target, it logs
that the installed version is newer and returns success without calling backup,
constructing upgrade managers, updating programs, or persisting version metadata.
When the stored value is non-empty but absent from the configured sequence, it
logs a compatibility error and returns failure through the same mutation-free
preflight boundary. Missing/unavailable metadata is allowed past this preflight
so the existing legacy behavior remains separate.

`DataBaseManager.update_version()` also defends the persistence boundary: if
called directly while the stored current version follows the target, it returns
success without changing `current_version`, `last_running_version`, or
`update_time`. If the stored version is absent from the configured sequence, it
returns failure without changing any of those rows. This prevents downgrade or
unvalidated persistence even outside the main orchestrator.

The checked-in `resources/version.yml` will initially contain exactly the version sequence and target currently hard-coded in `consts/program_config.py`.

## Error Handling

Missing, unreadable, malformed, or semantically invalid configuration is fatal. The loader raises a descriptive configuration error that includes the configuration path and the validation reason. The application must not silently use defaults, infer values, partially execute an upgrade, or persist a target version after configuration loading fails.

Because loading occurs when application configuration is initialized, failure happens before construction of the upgrade managers and before database or filesystem mutation.

## Packaging and Deployment

`resources/version.yml` remains an external deployment file beside the other `resources` content. It is not embedded into the executable as an immutable Python value. The current executable build command is `pyinstaller -F main.py`, which produces `dist/main.exe` but does not assemble editable sidecar resources.

This feature adds a checked-in PowerShell release-build script as the supported
assembly entry point. The script runs the existing `pyinstaller -F main.py`
command, creates a clean product staging directory under `dist`, places
`main.exe` at its root, and copies the repository's external runtime `resources`
content into the staging directory, including `resources/version.yml`. The
script must fail if the source version configuration is absent or if the staged
file cannot be verified. It does not use `--add-data` for this file, because a
one-file embedded data asset would be extracted to a temporary directory and
would not be the editable executable-relative configuration required here.

The real staged artifact is verified after the copy: it must contain
`main.exe` and `resources/version.yml` under the same package root, and the
configuration-loading path must resolve that staged external file. The exact
staging-directory name will be fixed in the implementation plan and documented
by the script usage; existing raw `dist/main.exe` output remains available as an
intermediate build product.

## Testing

Tests will cover:

- The checked-in YAML reproduces the current ordered version sequence and target.
- A valid temporary YAML file loads successfully and preserves declared order.
- Missing files, invalid YAML, non-mapping documents, empty version lists,
  whitespace-padded or path-unsafe entries, duplicate versions, omission of the
  legacy `0.12` version, invalid targets, and targets absent from the list are
  rejected with useful diagnostics.
- Upgrade selection follows declared list order, including lexically misleading
  identifiers, stops at a nonterminal target, rejects an unknown current version,
  and returns no manifest work when current is at or after the target.
- When current follows a nonterminal target, the main orchestration returns
  success without backup or mutation, and direct version persistence retains all
  existing version metadata without downgrade.
- An unknown stored current version is rejected by preflight and never invokes
  backup, database restore, manager construction, or any other
  mutation; unavailable legacy metadata retains its existing orchestrated failure
  and standalone initialization behavior.
- Direct persistence rejects an unknown current version without modifying
  `current_version`, `last_running_version`, or `update_time`.
- Database version persistence writes the externally configured target.
- The checked-in PowerShell release-build script invokes `pyinstaller -F main.py`,
  produces the documented real staging layout, and verifies the editable external
  `resources/version.yml` beside the executable.
- Relevant existing upgrade orchestration and database/config upgrade tests continue to pass.

## Compatibility / Migration

- **Backward compatibility:** required
- **Protected surfaces:** Python module constants `ALL_VERSIONS` and `TARGET_VERSION`; declared upgrade ordering; target-boundary behavior; no-downgrade behavior for newer installed versions; legacy `0.12` database initialization; CLI behavior for valid releases; database schema and version metadata behavior; existing per-version manifest format; the underlying `pyinstaller -F main.py` executable build
- **Allowed breakage:** deployments that omit `resources/version.yml` or provide invalid content will now fail before upgrading; directly editing version values in Python source is no longer supported
- **Migration strategy:** add `resources/version.yml` containing the former hard-coded values and include it in every release package; no database or user-data migration is required

## Acceptance Criteria

- No literal upgrade sequence or target release value remains as the runtime source of truth in Python production code.
- Both configuration-upgrade selection and database-upgrade selection use values loaded from `resources/version.yml`.
- Upgrade selection is index-based and therefore honors the external sequence
  even when identifier lexical order differs from configured order.
- Database version persistence uses the externally loaded target.
- Running an older package against a known newer installed version succeeds as a
  no-op and never downgrades stored metadata.
- Invalid external configuration prevents upgrade-side mutations and produces a clear failure.
- Valid existing deployments behave the same after the new file is included.
- The supported release-build script runs `pyinstaller -F main.py` and assembles
  a verified product directory containing an editable `resources/version.yml` at
  the executable-relative lookup location.
