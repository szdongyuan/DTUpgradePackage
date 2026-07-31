# Missing Config Source Delete Compatibility Design

## Purpose

Restore the legacy upgrade-package behavior in which an entry under a version
manifest's `config` list deletes its resolved target when the corresponding
upgrade-package source path does not exist.

The restored behavior is supported only by the Windows upgrade executable. It
must keep the current implementation's managed-root path restrictions,
link/reparse-point escape defenses, backup behavior, rollback behavior, and
fail-closed handling for deletion errors. It must not restore the legacy
implementation wholesale.

Windows compatibility deletion binds mutation to the exact destination object
with native file handles. Non-Windows platforms fail the compatibility-delete
operation before backup or target mutation because their portable pathname APIs
cannot atomically couple the verified object identity to rename and cleanup.

## Current Behavior

For each `config` entry, `ConfigManager.update_config_file()` currently:

1. Resolves and validates the destination under one of the managed roots.
2. Resolves a source beneath `resources/config` using the destination basename.
3. Fails the configuration stage with `source_path:missing_path` when that
   source does not exist.
4. Backs up managed configuration roots only after all entries pass preflight.
5. Replaces each destination through the staged ordinary-config replacement
   path.

This prevents an existing manifest such as `config: ["ui/ui_pic"]` from
expressing the legacy delete operation when `resources/config/ui_pic` is
intentionally absent.

## Required Behavior

### Manifest Semantics

The existing YAML schema remains unchanged. Each value in `config` has two
possible operations:

- If its resolved source exists and is a supported file or directory, replace
  the resolved destination using the current staged replacement behavior.
- If source inspection proves that the expected lexical source entry does not
  exist, report `missing_path` internally and delete the resolved destination
  using the compatibility behavior described below.

All other source-resolution errors remain fatal, including invalid paths,
source symlink escapes, and unsupported source types.

For compatibility deletion, `missing_path` means verified lexical absence:
`os.path.lexists()` is false and a non-following identity lookup confirms that
no directory entry exists. A dangling symlink, junction, reparse point,
permission failure, transient I/O failure, or indeterminate identity result is
not a missing source and must fail closed.

Within one version manifest, any operation set containing a compatibility
deletion must not also contain a duplicate or ancestor/descendant target after
normalization and identity resolution. Those deletion-related conflicts are
rejected during preflight because their result would be order-dependent.
Replacement-only entries retain their existing sequencing behavior. Manifests
from different versions continue to execute in version order, preserving
historical upgrade sequencing.

A compatibility deletion also conflicts with every same-manifest `json_merge`
target that is equal to, an ancestor of, or a descendant of the deletion target.
These conflicts are rejected before the containing manifest performs backup or
mutation.

### Platform Support

- On Windows, a verified-missing source selects compatibility deletion.
- On non-Windows platforms, the same manifest entry fails during preflight with
  a non-sensitive `delete_compatibility:unsupported_platform` category.
- The non-Windows failure occurs before the containing version manifest performs
  backup, artifact creation, target mutation, or JSON merge. Earlier version
  manifests may already have executed in version order; the resulting
  configuration-stage failure invokes the existing top-level rollback, which
  restores their selected managed-root backups.
- Existing-source replacement behavior remains cross-platform and unchanged.
- Windows compatibility deletion requires Windows 10 version 1709 or newer (or
  the corresponding Windows Server release) and a local filesystem supporting
  handle rename plus `FileDispositionInfoEx`. Missing native/API/filesystem
  support fails closed before mutation.

### Delete Compatibility Flow

For a `config` entry whose source is missing:

1. Destination validation must succeed through
   `_resolve_ordinary_config_destination`.
2. Preflight verifies that compatibility deletion is running on Windows and
   records whether the destination exists, the identities of each existing
   ancestor from the managed root to the destination parent, the destination
   identity and type when present, and a stable content manifest when the
   destination is a file or directory. It also records the trusted source
   directory chain from the updater root through `resources/config`.
3. The managed root containing the target must be backed up successfully before
   any deletion when that managed root exists.
4. When the deletion target is verified absent and its containing managed root
   is also verified absent, no backup is required: the operation is an
   idempotent no-op and no backup flag is changed. This covers both the managed
   root itself and an absent descendant of an absent managed root.
5. Immediately before deletion, the program reacquires the destination ancestor
   chain with Windows directory handles that deny delete-sharing, verifies each
   handle identity against preflight, and opens the exact destination with a
   native handle carrying the rights required for rename/deletion. Existing
   target identity swaps, type changes, content changes, and absent-to-present
   transitions are rejected.
6. The expected source must again be proven absent immediately before mutation
   relative to a verified, held `CONFIG_DIR` handle. If it has appeared or its
   state is indeterminate, the operation fails closed; it does not switch
   dynamically to replacement.
7. Ancestor and exact-target handles remain held through destination detach,
   artifact verification, cleanup or local restoration, and postcondition
   verification.
8. If the destination was and remains absent, the operation succeeds as an
   idempotent no-op.
9. If the destination exists and remains bound to the recorded state, only that
   exact file or directory is removed through the atomic-detach algorithm below.
10. A successful deletion counts as a successful configuration action and the
   upgrade proceeds to later configuration, executable, program, and database
   version stages.
11. A deletion failure fails the configuration stage and triggers the existing
   upgrade rollback path.

### Atomic Detach and Cleanup

An existing destination is not recursively deleted in place:

1. Open and validate a dedicated recovery directory under
   `BACKUP_DIR/recovery`. The recovery directory must be inside the trusted
   updater root and on the same Windows volume as the destination. If these
   conditions cannot be proved, fail before mutation.
2. Allocate a unique, nonexistent recovery-artifact name inside that bound
   recovery directory.
3. Recheck source absence, ancestor-handle identities, the exact target-handle
   identity, and the stable manifest.
4. Rename the exact open target object to the recovery-artifact path with
   Windows handle-based rename information and no replacement.
5. Keep the same exact-object handle open after rename; verify that the original
   destination name is absent and that the object still matches the captured
   identity, type, and stable manifest.
6. Clean artifact contents without following reparse points while the artifact
   root and ancestors remain handle-bound. Delete the artifact root using native
   handle disposition rather than a final pathname-only removal.
7. Report success only when the destination remains absent, the exact artifact
   object is deleted, and no recovery artifact remains.

The recovery artifact uses a collision-resistant name derived from the
destination basename. An artifact-name collision is retried with a new name and
never replaces an existing entry.

### Windows Handle Protocol

The implementation must use native handles as follows:

- Open every destination ancestor and the recovery directory with
  `FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY`, share only
  `FILE_SHARE_READ | FILE_SHARE_WRITE` (specifically omit
  `FILE_SHARE_DELETE`), and use `FILE_FLAG_BACKUP_SEMANTICS |
  FILE_FLAG_OPEN_REPARSE_POINT`.
- Open and identity-check every source-directory ancestor from the trusted
  updater root through `resources/config` with the same no-delete-sharing
  directory-handle protocol. Hold these handles through the final source
  absence check and exact-target rename.
- Open the target root and every cleanup descendant with the minimum required
  `DELETE`, attribute, enumeration, and read rights; omit `FILE_SHARE_DELETE`;
  use `FILE_FLAG_OPEN_REPARSE_POINT` so a reparse entry is never followed.
- Query identity from each handle using volume serial number plus 128-bit file
  ID (`FileIdInfo`), file type/attributes, and reparse tag. Match these fields to
  preflight/captured manifest data before mutation.
- Enumerate directories from their bound handles. Open descendants relative to
  the verified parent handle using a Windows native relative-open operation
  (`NtCreateFile` with the parent as `RootDirectory`), then revalidate the
  opened child identity before reading, recursing, or deleting it.
- Prove final source absence by attempting a handle-relative non-following open
  of the expected basename with `NtCreateFile` using the verified
  `CONFIG_DIR` handle as `RootDirectory`. Only the exact NT statuses for an
  absent final name count as missing. Missing source parents, access failures,
  reparse results, unexpected statuses, or an opened source handle fail closed.
- Rename the exact destination handle into the recovery namespace using
  `FILE_RENAME_INFO`/`FILE_RENAME_INFO_EX` with the verified recovery-directory
  handle in `RootDirectory`, the artifact basename as the relative name, and
  replacement disabled. A full pathname-only rename destination is not used.
- Hash regular files from their verified handles. Reparse entries are never
  traversed; compatibility deletion of a destination root that is itself a
  reparse point is rejected during preflight.
- Delete descendants leaf-first by setting
  `FileDispositionInfoEx` with delete, POSIX-semantics, and ignore-readonly
  flags on the exact verified handle, then close that descendant handle before
  continuing. Re-enumerate the parent before setting its disposition; any
  unexpected or changed entry fails cleanup.
- Set `FileDispositionInfoEx` on the exact artifact-root handle only after the
  directory is verified empty (or immediately for a file), then close the root
  handle. Logical deletion linearizes when disposition is accepted and the
  handle is closed. Keep ancestor/recovery-directory handles open, verify the
  artifact name and original destination name are absent, and only then report
  success.
- If `FileDispositionInfoEx` or required handle-relative operations are
  unavailable or return unsupported-filesystem errors, fail closed. Do not fall
  back to pathname-only `smart_remove`.
- Close order is leaf handles first, then the artifact-root handle after
  disposition, followed by recovery-directory and destination-ancestor handles
  only after postcondition verification. Every exceptional path closes handles
  in reverse acquisition order.

Source absence and absent destination names are guaranteed at their final
handle-bound checks. Source absence linearizes at the verified
`CONFIG_DIR`-relative open attempt immediately before exact-target rename. A
new entry created after the corresponding linearization point is a subsequent
concurrent operation and is outside this deletion operation's guarantee; it
must not be mistaken for proof that the exact handle-bound deletion acted on
the wrong object.

### Transaction and Rollback Semantics

- The existing per-managed-root backup remains the recovery source.
- If any later upgrade stage fails, `restore_config()` restores the managed
  root, including a target removed by compatibility deletion.
- If the full upgrade succeeds, normal backup cleanup makes the deletion
  permanent.
- Deletion must not be reported as successful until the target is absent.
- A cleanup or recovery artifact needed after an indeterminate deletion failure
  must be preserved and reported rather than silently discarded.

Deletion outcomes are handled as follows:

- Failure before atomic detach leaves the target untouched and returns failure.
- If detach succeeds but identity, manifest, or postcondition verification
  fails, the helper attempts local restoration only while the original exact
  artifact handle still identifies the captured destination object, the
  destination remains absent, and verified ancestor handles remain held through
  a no-clobber exact-handle rename. Otherwise it preserves the artifact and
  proceeds to global rollback.
- If recovery-artifact cleanup fails before changing the artifact, the helper
  attempts the same fully revalidated and bound local restoration and returns
  failure.
- If cleanup partially removes the artifact, exact local restoration is no
  longer assumed possible. The remaining artifact is preserved, its path is
  logged, and the helper returns failure so global managed-root rollback runs.
- If local restoration fails, the artifact is preserved, its path is logged,
  and the helper returns failure so global rollback runs.
- If global rollback also fails, all remaining recovery and backup artifacts
  are retained and their paths are logged for manual recovery.
- Success is returned only for a verified absent no-op or a completed detach,
  verification, and artifact cleanup sequence.

## Security Invariants

The change must preserve all of the following:

- Only destinations beneath the existing managed roots are accepted:
  `ui/ui_config`, `ui/ui_pic`, `configs/aimodel_config`, and
  `configs/scanner_barcode_config`.
- A managed root itself, such as `ui/ui_pic`, is a valid destination.
- Absolute, drive-qualified, rooted, or lexically out-of-root paths remain
  rejected.
- Symlink, junction, and reparse-point escapes remain rejected.
- A deletion target whose root entry is itself any symlink, junction, or other
  reparse point is rejected with
  `delete_compatibility:unsupported_destination_reparse`; the link object is not
  deleted and its target is never traversed.
- A destination or ancestor substitution between validation and deletion must
  not permit mutation outside the originally validated managed target.
- Existing-target swaps, type/content changes, and absent-to-present races
  between preflight and mutation are rejected.
- Windows rename, local restoration, and artifact-root deletion act on the
  exact previously verified open object, not a subsequently resolved pathname.
- Ancestor handles deny delete-sharing for their lifetime so the artifact
  namespace cannot be rebound during mutation or recovery.
- Source-directory handles deny delete-sharing and final source absence is
  checked relative to the verified `CONFIG_DIR` object, so updater-source path
  substitution cannot select deletion.
- Existing source paths must never be interpreted as delete requests merely
  because copying, hashing, permission checks, or staged replacement later
  fails.
- Only twice-confirmed lexical source absence selects deletion semantics.
- Dangling source links and indeterminate source identity results remain fatal.
- Duplicate and ancestor/descendant targets in an operation set containing a
  compatibility deletion are rejected before backup or mutation.
- Equal and ancestor/descendant conflicts between a compatibility deletion and
  same-manifest JSON merge targets are rejected before backup or mutation.
- Non-Windows compatibility deletion is rejected before backup or mutation.

## Logging

The program must clearly distinguish compatibility deletion from replacement
and from failures. Logs and console output must identify:

- the manifest-relative target selected for compatibility deletion;
- successful deletion or already-absent no-op;
- deletion failure, without reporting configuration upgrade success.

The old misleading `source_path:missing_path` error must not be emitted for a
successful compatibility deletion.

## Code Structure

`control/upgrade_config.py` remains the implementation location.

- Preflight records each `config` entry as either a replacement operation or a
  missing-source deletion operation.
- Backup remains centralized in `backup_config_file`.
- A dedicated ordinary-config deletion helper owns the final destination
  and source revalidation, identity/binding checks, atomic detach, cleanup,
  failure recovery, and reporting through Windows native handles. The
  general-purpose `smart_remove` helper is not sufficient on its own because it
  does not bind mutation to the exact destination identity checked during
  preflight.
- Native handle wrappers are small, testable helpers responsible for Windows
  absolute/relative open, handle enumeration/read, identity query, no-replace
  rename, delete disposition, and reliable handle close.
- Existing staged replacement code remains unchanged for entries whose sources
  exist.

No new YAML field or migration script is introduced.

## Testing

Automated tests in `tests/test_upgrade_config.py` must cover:

1. Missing directory source deletes an existing managed directory and
   `update_config_file()` returns `True`.
2. Missing file source deletes an existing managed file.
3. Missing source with an already-absent destination succeeds as a no-op.
4. An already-absent managed-root target succeeds without attempting to back up
   a nonexistent root or changing its backup flag.
5. Existing source still performs replacement rather than deletion.
6. A source that appears after preflight fails closed without deletion.
7. Dangling source links, reparse points, permission errors, and indeterminate
   source states remain fatal and preserve the target.
8. Non-`missing_path` source errors remain fatal and preserve the target.
9. Backup failure prevents deletion.
10. Deletion failure before detach preserves the target.
11. Post-detach verification failure attempts local restoration and returns
    `False`.
12. Partial artifact cleanup and failed local restoration preserve all
    recoverable artifacts, log their paths, and return `False`.
13. A later upgrade failure restores a compatibility-deleted target from the
   managed-root backup.
14. Managed-root deletion, specifically `ui/ui_pic`, is accepted.
15. Parent symlink/junction substitution cannot delete an external target.
16. Existing-target swaps, type/content changes, and absent-to-present
    destination races are rejected.
17. Duplicate and ancestor/descendant targets in a manifest operation set that
    contains a compatibility deletion are rejected before backup.
18. Recovery-artifact substitution prevents local restoration, retains the
    untrusted artifact for inspection, and falls back to global rollback.
19. Logs distinguish compatibility deletion, no-op, retained recovery
    artifacts, and failure.
20. A target swapped at the final rename boundary remains at its destination and
    is not moved or deleted, because rename acts on the already-open exact
    object.
21. An artifact-name entry substituted after verification is not deleted in
    place of the trusted object; cleanup and root deletion remain bound to the
    exact open artifact.
22. Windows ancestor handles prevent parent rename/rebinding throughout detach,
    cleanup, and recovery.
23. Non-Windows missing-source compatibility deletion fails before backup and
    mutation with `delete_compatibility:unsupported_platform`.
24. Missing required Windows APIs or unsupported filesystems fail before target
    mutation without pathname-only fallback.
25. Destination-root reparse points are rejected without deleting the link or
    traversing its target.
26. A deletion target equal to, above, or below a same-manifest JSON merge
    target is rejected before backup.
27. An absent descendant of an absent managed root succeeds as an unbacked
    no-op without changing the managed-root backup flag.
28. Recovery artifacts live under `BACKUP_DIR/recovery`, survive managed-root
    global rollback, and are removed only after successful exact-handle cleanup.
29. Descendant substitution between enumeration and relative open is rejected;
    no substituted child is deleted as though it matched the captured manifest.
30. Artifact-root deletion is not reported successful until disposition,
    root-handle close, and bound-directory name-absence verification complete.
31. Substitution of `CONFIG_DIR` or any source-directory ancestor between
    preflight and mutation fails closed; final absence is checked relative to
    the recorded source-directory object.
32. Exact-target rename resolves the recovery artifact basename relative to the
    verified recovery-directory handle and cannot be redirected by recovery
    path substitution.

The focused tests and the complete existing test suite must pass before a new
Windows executable is packaged. Windows-native race tests must execute rather
than being satisfied only through mocked POSIX branches.

## Compatibility / Migration

- **Backward compatibility:** partial
- **Protected surfaces:** existing version YAML `config` lists, managed-root
  path restrictions, upgrade rollback, console/log behavior, existing
  replacement semantics, Windows upgrade behavior
- **Allowed breakage:** a missing source no longer fails with
  `source_path:missing_path` on Windows and intentionally selects deletion
  compatibility; non-Windows missing-source deletion fails with
  `delete_compatibility:unsupported_platform`
- **Migration strategy:** compatibility layer; existing manifests require no
  schema changes for the Windows executable

## Out of Scope

- Restoring the complete historical `smart_remove`-then-`smart_copy`
  implementation.
- Allowing arbitrary filesystem paths to be deleted.
- Treating copy or replacement failures as successful deletions.
- Adding a separate `config_delete` YAML key.
- Supporting missing-source compatibility deletion on non-Windows platforms.
- Changing JSON merge, executable upgrade, database upgrade, or main-program
  replacement semantics.
