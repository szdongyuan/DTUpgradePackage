# Main Program `_internal` Bundle Replacement Design

## Objective

Extend the existing main-program upgrade stage so that an optional `_internal` directory beside `resources/DiTing.exe` is copied to the deployed main-program directory. When present, `DiTing.exe` and `_internal` must be replaced as one recoverable bundle. When absent, the upgrade remains backward compatible and replaces only `DiTing.exe`.

## Source and Destination

- Source executable: `DEFAULT_DIR/resources/DiTing.exe`.
- Source dependency directory: `DEFAULT_DIR/resources/_internal`.
- Destination executable: `DEFAULT_TARGET_DIR/DiTing.exe`.
- Destination dependency directory: `DEFAULT_TARGET_DIR/_internal`.

The executable source must be a real, readable regular file. A missing path, directory, symbolic link, broken link, or other non-regular object fails validation before destination mutation. Read/copy errors also fail before commit.

The source dependency path is classified with no destination mutation:

- Missing, including no filesystem entry at the path: log an informational skip and leave the destination `_internal` unchanged.
- Existing real directory: include it in the bundle replacement.
- Symbolic links, Windows junctions/reparse-point directory aliases, broken links, non-directories, and otherwise invalid directory objects: fail the main-program stage without modifying either destination object.

Directory copying does not intentionally follow source symbolic links or junctions. Encountering a link or junction anywhere inside the stable source tree is a validation failure before commit; only files and real directories rooted within the source `_internal` tree are copied. The source package is required to remain unchanged during one serialized invocation. Protection against another process replacing a source entry in the final check-to-enumeration instant requires platform-specific handle traversal and is outside this change.

## Transactional Replacement

`update_version_program()` owns the bundle transaction.

1. Validate the executable source and classify the optional `_internal` source.
2. Copy the new executable into a unique staging file beside the destination executable.
3. When `_internal` is present, copy its full tree into a unique staging directory beside the destination `_internal`.
4. Only after all required staging copies succeed, move any existing destination executable to a unique sibling recovery path. Move an existing destination `_internal` object only when a valid source `_internal` is participating in this invocation.
5. Install the staged `_internal` directory, when present, and the staged executable at their final paths.
6. If any transition or installation step fails, remove only newly installed objects and restore every displaced destination object from its recovery path.
7. On every exit, clean all staging artifacts created by the current invocation. On success, also clean displaced recovery artifacts. Cleanup failure is logged with the retained artifact path. A pre-commit staging/validation failure remains a failed upgrade; a cleanup failure after an already committed, internally consistent bundle does not change success into failure.

The destination `_internal` is a full replacement. Files that existed only in the old directory are not retained. If the source `_internal` is absent, the destination `_internal` is not staged, displaced, deleted, or changed.

## Failure Semantics

- Any validation, staging-copy, displacement, installation, or required rollback failure returns `(False, message)` from `update_version_program()`.
- A normal replacement failure restores the prior executable and prior `_internal` before returning failure.
- If recovery itself is incomplete, the error message and log explicitly identify retained recovery artifacts.
- Partial executable or directory staging created before a copy failure is removed before returning. If removal fails, the failure message and log identify the retained staging artifact.
- The existing `run_upgrade()` orchestration treats the returned failure as a main-program-stage failure and performs its existing database/configuration rollback.
- Logging and console-reporting failures remain best effort and do not change a successful or failed filesystem transaction result.
- The first `BaseException` control-flow signal encountered triggers best-effort transaction rollback/cleanup and is then re-raised unchanged; it is never converted into a normal success/failure return. Secondary rollback/cleanup failures are reported best effort and do not replace that first control-flow signal. If an ordinary failure is already being handled and rollback/cleanup raises the first `BaseException`, that control-flow signal propagates after remaining best-effort recovery attempts.

The transaction guarantee covers one serialized invocation with a stable source package and failures represented by caught Python exceptions. Cross-process locking, concurrent source mutation, recovery after abrupt process termination or power loss, and reconciliation of artifacts from older invocations are outside this change. The existing upgrade launcher is responsible for preventing concurrent upgrade invocations.

## Safety Boundaries

- Staging and recovery paths are unique siblings under the fixed deployed program directory.
- Operations use resolved, fixed source/destination roots; no manifest-provided path is introduced.
- Existing destination directory-like objects are displaced as objects rather than traversed before replacement.
- Cleanup targets only artifacts created by the current invocation.
- Staging/recovery links, broken links, and junctions are removed as leaf objects; cleanup never traverses their referents. Real staging/recovery directories owned or displaced by the invocation may be recursively removed.
- When source `_internal` is absent, any destination `_internal` object—including a directory, file, link, or broken link—is left untouched.

## Testing

- Missing source `_internal`: executable replaced, existing destination `_internal` unchanged, success returned, skip logged.
- Present source `_internal` with no destination: executable and full directory installed.
- Present source `_internal` with existing destination: complete replacement; stale target-only files removed.
- Invalid source `_internal` object: failure before destination mutation.
- Source `_internal` link/junction and nested link/junction: failure before destination mutation.
- Missing, invalid, linked, or unreadable executable source: failure before destination mutation.
- Executable staging failure and directory staging failure: both destinations unchanged.
- Partial staging artifacts are removed; retained artifacts are reported if cleanup fails.
- Failures while displacing or installing either object: prior executable and directory restored, no mixed-version bundle remains.
- Rollback failure reports retained recovery paths without deleting the only recoverable prior object.
- Cleanup-reporting failures do not mask a successful committed bundle.
- Logger/console failures remain isolated; control-flow exceptions propagate.
- `BaseException` at staging, displacement, installation, rollback, or cleanup boundaries attempts all remaining recovery/cleanup before the first control-flow signal propagates unchanged.
- Recovery cleanup of displaced link/junction objects leaves every external referent untouched.
- Absent source `_internal` leaves destination `_internal` unchanged for directory and non-directory destination objects.
- Existing top-level upgrade tests continue to prove that main-program-stage failure triggers orchestration rollback.

## Compatibility / Migration

- Backward compatibility: required.
- Protected surfaces: upgrade CLI result, `update_version_program()` return contract, packages without `_internal`, target `_internal` contents when the source directory is absent, database/config rollback orchestration.
- Allowed breakage: when a source `_internal` is present, stale files from an existing destination `_internal` are intentionally removed by full replacement.
- Migration strategy: automatic optional bundle detection; no manifest or database migration.
