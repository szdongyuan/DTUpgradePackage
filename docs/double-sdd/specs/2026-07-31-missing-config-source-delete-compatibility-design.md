# Missing Config Source Delete Compatibility Design

## Purpose

Restore the legacy upgrade-package behavior in which an entry under a version
manifest's `config` list deletes its resolved target when the corresponding
upgrade-package source path does not exist.

The restored behavior must keep the current implementation's managed-root path
restrictions, link/reparse-point escape defenses, backup behavior, rollback
behavior, and fail-closed handling for deletion errors. It must not restore the
legacy implementation wholesale.

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

### Delete Compatibility Flow

For a `config` entry whose source is missing:

1. Destination validation must succeed through
   `_resolve_ordinary_config_destination`.
2. Preflight records whether the destination exists, the identities of each
   existing ancestor from the managed root to the destination parent, the
   destination identity and type when present, and a stable content manifest
   when the destination is a file or directory.
3. The managed root containing the target must be backed up successfully before
   any deletion when that managed root exists.
4. When the deletion target is the managed root itself and that managed root is
   verified absent, no backup is required: the operation is an idempotent no-op
   and no backup flag is changed.
5. Immediately before deletion, the program must reacquire/bind the destination
   ancestor chain and verify it against the recorded state. Existing-target
   identity swaps, type changes, content changes, and absent-to-present
   transitions are rejected.
6. The expected source must again be proven lexically absent immediately before
   mutation. If it has appeared or its state is indeterminate, the operation
   fails closed; it does not switch dynamically to replacement.
7. Ancestor bindings/handles remain held through the atomic destination detach
   and postcondition verification.
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

1. Allocate a unique, nonexistent recovery-artifact path next to the
   destination.
2. Recheck the source absence and destination/ancestor bindings.
3. Atomically rename the destination to the recovery-artifact path without
   replacing any existing entry.
4. Verify that the destination is absent and that the recovery artifact matches
   the exact identity, type, and stable manifest captured for the destination.
5. Remove the recovery artifact using the existing snapshot-artifact cleanup
   rules.
6. Report success only when the destination remains absent and recovery-artifact
   cleanup completes.

The recovery artifact uses a collision-resistant name derived from the
destination basename and remains on the same filesystem so the detach can be
atomic.

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
  fails, the helper attempts local restoration only if the recovery artifact is
  independently revalidated against the recorded destination identity and
  stable manifest, the destination remains absent, and safe ancestor bindings
  are reacquired and held through a no-clobber atomic restoration. Otherwise it
  preserves the artifact and proceeds to global rollback.
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
- A destination or ancestor substitution between validation and deletion must
  not permit mutation outside the originally validated managed target.
- Existing-target swaps, type/content changes, and absent-to-present races
  between preflight and mutation are rejected.
- Existing source paths must never be interpreted as delete requests merely
  because copying, hashing, permission checks, or staged replacement later
  fails.
- Only twice-confirmed lexical source absence selects deletion semantics.
- Dangling source links and indeterminate source identity results remain fatal.
- Duplicate and ancestor/descendant targets in an operation set containing a
  compatibility deletion are rejected before backup or mutation.

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
  failure recovery, and reporting. The general-purpose `smart_remove` helper is
  not sufficient on its own because it does not bind the mutation to the
  destination identity checked during preflight.
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

The focused tests and the complete existing test suite must pass before a new
executable is packaged.

## Compatibility / Migration

- **Backward compatibility:** required
- **Protected surfaces:** existing version YAML `config` lists, managed-root
  path restrictions, upgrade rollback, console/log behavior, existing
  replacement semantics
- **Allowed breakage:** a missing source no longer fails with
  `source_path:missing_path`; it intentionally selects deletion compatibility
- **Migration strategy:** compatibility layer; existing manifests require no
  schema changes

## Out of Scope

- Restoring the complete historical `smart_remove`-then-`smart_copy`
  implementation.
- Allowing arbitrary filesystem paths to be deleted.
- Treating copy or replacement failures as successful deletions.
- Adding a separate `config_delete` YAML key.
- Changing JSON merge, executable upgrade, database upgrade, or main-program
  replacement semantics.
