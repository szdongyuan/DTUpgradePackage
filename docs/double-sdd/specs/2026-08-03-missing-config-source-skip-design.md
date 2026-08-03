# Missing Ordinary Config Source Skip Design

## Context

The ordinary configuration upgrade path was extended to interpret a missing
package source as a request to delete the installed target. That behavior led
to validation, backup, deletion, and recovery failures for manifests that had
historically listed optional configuration paths without shipping a matching
source.

Git history shows that JSON merge support (`91f134a`) predates the
missing-source deletion compatibility work. No historical commit implements
the exact policy selected here: `f20de0d` rejected a missing source before
backup, while older `91f134a` could remove the target before a failed copy. The
new compatibility policy combines the user's required no-mutation guarantee
with the established replacement-only interpretation of ordinary entries.

## Selected Approach

Restore replacement-only semantics in the current implementation rather than
resetting the entire module to an old commit.

For every ordinary `config` entry:

1. Validate the manifest entry's basic syntax with the existing
   `_ordinary_config_path_error` rules. Non-string, empty, NUL-containing,
   rooted, absolute, and drive-qualified entries remain fatal even when their
   derived source is absent. Preserve the existing accepted lexical aliases,
   including `.` and an internal `sub/..`; if their source exists, the later
   destination resolver remains responsible for allowed-root containment.
2. Derive the package source directly from the validated manifest entry using
   the established basename mapping:
   `abspath(join(CONFIG_DIR, basename(normpath(config_entry))))`. This step must
   not resolve or inspect the installed destination.
3. Validate the trusted package source parent chain. A missing or unsafe
   `DEFAULT_DIR`, `resources`, or `resources/config` parent remains fatal.
4. If only the final source leaf is absent (`missing_path`), emit an
   informational skip message and continue.
   The entry must not enter target conflict checks, backup selection,
   replacement, deletion, or rollback state.
5. If the source exists but is invalid or unsafe, retain the existing fail-closed
   error behavior.
6. For a valid source, retain the current destination validation, staged
   replacement, backup, and rollback behavior.
7. Remove missing-source delete operations from the active upgrade flow.
   Low-level deletion helpers may remain dormant to minimize unrelated risk.
8. Apply JSON merge entries after ordinary replacements as today. Conflict
   checks consider only ordinary entries that will actually be replaced.

This is preferred over two alternatives:

- Resetting `control/upgrade_config.py` wholesale to `91f134a` would discard
  unrelated rollback, reporting, and race-handling fixes.
- Keeping deletion compatibility behind a flag would retain the failure-prone
  path and add configuration complexity without a current use case.

## Data Flow

For each version manifest:

1. Parse `config` and `json_merge` sections.
2. Preflight each ordinary source.
3. Record missing ordinary sources as skipped and valid sources as replacement
   operations.
4. Run ordinary/JSON conflict checks using replacement operations only.
5. In the ordinary-operation backup phase, back up managed roots referenced by
   replacement operations only. JSON merge retains its independent per-target
   backup behavior.
6. Execute staged replacements.
7. Execute JSON merges.

## Error Handling

- Missing ordinary source leaf with a valid trusted parent chain: informational
  skip; version processing continues.
- Missing ordinary source parent: error because the package layout cannot be
  trusted.
- Invalid ordinary source (alias, reparse escape, identity failure, malformed
  path): error; no target mutation for that entry.
- Invalid destination for a source that exists: retain current error behavior.
- Backup or replacement failure for a source that exists: retain current
  rollback behavior.
- JSON schema, source, target, or merge failure: retain current error behavior.

Source classification is fixed for the current version-manifest pass. If a
source appears after it was classified as missing, the entry remains skipped.
For a present source, identity and content must remain consistent from
preflight through the staging copy and staged-content validation. A change in
that interval fails without modifying the installed target. Once the staged
snapshot has been validated against the original source manifest, it becomes
the immutable transaction input; later changes to the live package source do
not invalidate committing that already-validated snapshot. This avoids
triggering target rollback for an object that is no longer read by the
transaction.

## Testing

Automated tests must prove:

- A missing `ui/ui_pic` package source returns overall success and leaves the
  installed `ui_pic` tree byte-for-byte unchanged.
- Missing-source entries do not invoke backup, replacement, deletion, or delete
  compatibility helpers.
- Non-string, empty, NUL-containing, rooted, absolute, and drive-qualified
  manifest entries remain errors even when the basename source is absent.
- Existing dot-component and internal parent-component aliases remain accepted;
  a missing derived source skips them, while a present source proceeds through
  normal destination containment validation.
- A missing `resources/config` source parent remains an error.
- A source that appears after missing classification remains skipped.
- A source that disappears or changes identity before staged-snapshot
  validation completes fails without changing or deleting the installed
  target.
- A source that changes after staged-snapshot validation may coexist with a
  successful commit, but the installed target must equal the validated staged
  snapshot derived from the original source, never the later source object.
- A manifest mixing a missing source and a present source skips the former and
  replaces the latter.
- JSON merge still runs when an ordinary source is skipped.
- Existing source replacement, backup, rollback, invalid-source rejection, and
  JSON merge tests continue to pass.
- Top-level upgrade orchestration does not roll back solely because an ordinary
  configuration source is missing.

## Deployment Verification

Build a clean PyInstaller executable with the user's current target version
configuration, place it in
`C:/Users/Administrator/Desktop/20260417/20260701-1/main.exe`, and execute it
from that directory. Preserve the replaced executable as a timestamped backup.
Success requires no `delete_compatibility selected` message, no configuration
backup error for the missing `ui/ui_pic` source, and a successful overall
upgrade outcome.

## Compatibility / Migration

- Backward compatibility: required
- Protected surfaces: configuration manifest format, database upgrade flow,
  executable upgrade flow, backup/rollback behavior for real replacements,
  JSON merge format and behavior, console/log reporting
- Allowed breakage: manifests can no longer request deletion by omitting an
  ordinary configuration source
- Migration strategy: none; intentional deletions must use a future explicit
  deletion operation rather than source absence

## Decision Authority

The user explicitly delegated all remaining product and implementation
decisions and requested unattended completion. This document records the
selected compatibility posture and serves as the approved design for execution.
