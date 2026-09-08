# PR7 machine qualification and one-shot installation

PR7 System Mode is unavailable from release builds and fails closed in debug builds until one exact
target has a current, host-sealed qualification record. Typed boot counts, a backup filename,
historical notes, consent to proceed without recovery, and hand-authored JSON are not evidence.

## What qualification proves

Qualification uses two separate external backups.

1. It inventories the exact bytes and metadata of every durable PR7 boot-state root under
   `/system` and `/data/adb`.
2. It creates unpredictable anchors in both the selected persistent init filesystem and
   `/data/adb`, then creates a temporary challenge backup.
3. It overwrites both anchors, adds independent markers, and publishes a temporary init RC and
   helper. After the existing root provider initializes its policy, across three distinct cold boots,
   that RC executes the helper as both `u:r:init:s0`
   and `u:r:magisk:s0`. Each result contains the current random boot ID, so replaying a prior
   result cannot pass.
4. It restores the challenge backup and requires both original anchors, including their exact
   bytes, mode, owner, group, and SELinux metadata, to return. Post-backup files must disappear.
5. It removes every anchor, RC, helper, result, marker, scratch file, and qualification property,
   verifies the original inventory, and creates a second clean backup.
6. It adds fresh post-backup markers in both `/system` and `/data/adb`, restores the named clean
   backup, and requires both markers to disappear. The full canonical inventory and live target
   contract must exactly equal the pre-qualification baseline.

The challenged artifact is deleted after proof. Authorization binds only the second, residue-free
backup. A wrapper that backs up only `/system` or only `/data` cannot pass both challenges.

The inventory recursively covers the System Mode payload, selected and legacy init RCs, invariant
rescue tree, supported persistent SELinux policy locations and sidecars, addon.d payloads,
transaction receipts, `/data/adb/magisk`, Magisk databases and journals, modules and update trees,
post-fs-data/service scripts, and persistent sepolicy rules. Runtime tmpfs paths and cache logs are
not recovery artifacts and are not included.

The Magisk SQLite database is compared by its complete schema, typed rows, user version and
application ID after SQLite integrity checking. Legacy daemon startup can rewrite identical settings
while changing SQLite page counters and implicit rowids. Raw database byte hashes, sizes and mtimes
remain in the sealed record; permissions, ownership and SELinux labels must still match. A live WAL,
shared-memory file or rollback journal blocks the generic qualifier until checkpointed. External disk
backups retain their exact byte digests; other persistent files retain exact byte and metadata checks.

The record is authenticated with HMAC-SHA256 using a separate owner-only 32-byte host key. The key
is created and validated before the first guest mutation; it is never embedded in the record.
Editing or synthesizing JSON, using another key, weakening key permissions, or losing the key makes
the record unusable.

## Required inputs

- A new absolute final backup path outside the guest and outside every disk being modified. Its
  parent must already exist; the final path must not exist.
- Absolute, regular, non-symlink POSIX wrappers whose first line is exactly `#!/bin/sh`. Backup
  and restore command templates must each contain one standalone `{backup}` argument. The tool
  substitutes a private challenge path first and the named final path second.
- Wrappers must synchronously stop, back up or restore the exact instance, start it, and return only
  after that action is complete. They must use absolute dependencies. Each wrapper and the resolved
  `/bin/sh` interpreter are bound by path, device, inode, bytes, mode, ownership, size, and
  timestamps. The wrapper is read through one open descriptor and executed from that descriptor.
  On timeout, its entire process group is terminated and killed before recovery begins.
- An absolute non-symlink vendor metadata file uniquely identifying the emulator instance. A
  hand-written marker is not a valid instance identity.
- A separate owner-only seal key path, or the default per-user key at
  `~/.local/share/kitsune-magisk/qualification-v1.key`.
- Root-capable ADB, writable persistent `/system`, a supported init directory, and a clean exact
  `next-system` commit.

This PR7 generic qualifier intentionally requires the live target policy to already support the
Magisk domain. That matches the qualified legacy-Kitsune-to-PR7 MuMu upgrade path. A fresh target
whose root provider lacks `u:r:magisk:s0` fails closed even if the later PR7 launcher could inject
that domain; widening that flow requires an artifact-fed, adapter-specific policy qualification,
not an unproven shortcut.

The host verifier requires POSIX `openat`/`O_NOFOLLOW`, process-group, and `/dev/fd` semantics.
Native reparse-point-safe Windows lifecycle integration belongs to concrete commercial-emulator
adapters.

## Create a qualification

Store the record and seal key outside the repository:

```sh
python3 tools/system_mode/kitsune.py system-mode qualify \
  --connect 16384 \
  --output /absolute/external/path/pr7-qualification.json \
  --backup-location /absolute/external/path/new-vm0-clean-baseline \
  --snapshot-id vm0-pr7-clean-baseline \
  --instance-identity /absolute/vendor/path/vm0-metadata.json \
  --seal-key /absolute/private/path/qualification-v1.key \
  --backup-command "/absolute/path/backup-vm0 '{backup}'" \
  --cold-boot-command '/absolute/path/cold-boot-vm0' \
  --restore-command "/absolute/path/restore-vm0 '{backup}'"
```

Use port 16385 only after confirming that it is the same instance when 16384 is unavailable. Never
substitute an AVD or clone for the qualified target, and never delete the user-owned MuMu instance.

After the final clean restore:

```sh
python3 tools/system_mode/kitsune.py system-mode doctor \
  --connect 16384 \
  --qualification-record /absolute/external/path/pr7-qualification.json \
  --seal-key /absolute/private/path/qualification-v1.key \
  --json \
  --output /absolute/external/path/pr7-doctor.json
```

Doctor verifies the host seal, re-hashes the retained backup, revalidates the instance, wrapper and
interpreter identities, cross-checks all duplicated source/target/init fields, and requires the live
contract to match the post-restore baseline.

## Exact install handoff

With the exact debug APK built from the clean qualified commit:

```sh
python3 tools/system_mode/kitsune.py system-mode authorize \
  /absolute/external/path/pr7-doctor.json \
  --artifact /absolute/path/app-debug.apk \
  --seal-key /absolute/private/path/qualification-v1.key \
  --connect 16384
```

The command performs `adb install -r`, re-probes the pinned target, creates an ephemeral
transport-scoped `adb reverse` mapping, stages one authorization, and opens Install. Immediately
before mutation, the host reloads the exact doctor report, qualification record, seal key, backup,
instance identity, lifecycle wrappers, interpreter identities, APK inode and bytes, ADB serial, and
live target contract. Only then does it serve one random nonce. The guest deletes the authorization
before its first write. Timeout or cancellation removes the authorization and reverse mapping.

## PR7 completion boundary

Host tests and APK installation alone do not complete PR7. The exact qualified writable commercial
emulator must complete system install, three cold boots, upgrade/reinstall, module and MagiskSU
smoke, exact uninstall, and another external restore with the final clean commit and artifact. The
user-owned MuMu instance must never be deleted. If its external recovery artifact is absent,
destructive System Mode acceptance remains blocked even when the operator asks to proceed without a
backup.
