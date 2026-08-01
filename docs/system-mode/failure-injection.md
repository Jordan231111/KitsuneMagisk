# System Mode failure-injection plan

This plan defines the complete mutation boundaries exposed by the PR5B current-line transaction and
required again by the PR7 maintained-base port. PR5B implements the durable receipt,
manifest/journal, reverse recovery, fsync/rename boundaries, and exact uninstall. Its host suite
exercises the complete fault classes below; the 2026-08-01 MuMu qualification injected staged
process death live. Every boundary still requires live repetition on each target before a public
release claim.

Run every case from a disposable snapshot with an external backup whose digest and restore command
have already been verified. For each boundary, terminate the installer immediately after the
boundary is durably journaled, invoke automatic rollback, cold boot, and compare all original
digests and metadata. Then exercise the external restore even when automatic rollback succeeds.

| Boundary | Mutation allowed | Injected failure | Required rollback assertion |
|---|---|---|---|
| `preflight.complete` | None | Doctor/evidence validation error | No persistent path or journal exists |
| `backup.verified` | External backup only | Backup disconnect/digest mismatch | Target image remains byte-for-byte unchanged |
| `stage.created` | Staging area only | ENOSPC/process kill | Staging area is removable; target paths unchanged |
| `stage.payload` | Staging area only | Truncated payload/wrong ABI | Digest validation blocks commit |
| `stage.policy` | Staging area only | Parser error/invalid policy | Original policy is not opened for write |
| `stage.init` | Staging area only | RC syntax/import mismatch | Original init files are unchanged |
| `manifest.precommit` | Journal/manifest staging only | Manifest validation failure | No manifest is promoted and no target is changed |
| `commit.payload` | Exact manifest payload paths | I/O error/process kill | Reverse journal removes created files and restores replaced files |
| `commit.policy` | Selected policy path only | Short write/fsync failure | Original digest, mode, owner, timestamps, xattrs, and context are restored |
| `commit.init` | Dedicated RC/launcher path only | Short write/fsync failure | Dedicated path is removed/restored; unrelated RC files are untouched |
| `commit.manifest` | Manifest final path | Rename/fsync failure | Incomplete manifest cannot be treated as installed |
| `boot.first` | Committed installation | Service timeout/boot failure | Snapshot or recovery shell can run exact rollback without wildcard deletion |
| `boot.verify` | Committed installation | Wrong boot ID/root/module smoke failure | State becomes `ROLLBACK_REQUIRED`, never `BOOT_VERIFIED` |
| `uninstall.each-entry` | One owned path at a time | Missing/modified owned file | Hash mismatch is reported; unrelated files are never removed |
| `rollback.each-entry` | Reverse journal entry | Restore I/O error | State becomes `FAILED`; external restore command and backup digest are printed |

## Injection interface

The current transaction accepts the development-only
`KITSUNE_SYSTEM_MODE_FAIL_AT=<class>:<boundary>` form for `enospc`, `erofs`, `short-write`,
`fsync-file`, `fsync-parent`, `rename`, `process-death`, and `reboot`, plus an exact boundary name
for a normal injected failure. The release manager does not expose System Mode, and qualification
must use a debug artifact with explicit host authorization. Each boundary is emitted only after the
prior operation and journal record are durable.

The harness records:

- source artifact SHA-256 and commit;
- adapter/image/snapshot identifiers and sanitized doctor output;
- injected boundary and installer state before termination;
- original, staged, committed, rollback, and post-cold-boot digests;
- boot duration, root/module checks, SELinux state, and relevant AVCs;
- automatic rollback result and independent external restore result.

Passing shell parsing, an `rw` mount, or a successful warm reboot cannot satisfy a boundary. Every
post-commit case requires a real Android cold boot; release qualification additionally requires the
host lifecycle declared for that adapter.
