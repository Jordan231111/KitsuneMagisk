# System Mode machine qualification and one-shot installation

System Mode is available in release and debug builds from code 31022. Both refuse installation until one exact
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

Magisk also regenerates `/data/adb/sepolicy.rule` during a normal boot. Its observed modification
time is retained in the record but excluded from comparison; its bytes, size, mode, ownership and
SELinux label must still match. Timestamps of unrelated persistent files remain significant.
The observed MuMu ramdisk `/sepolicy` copy has the same exception only when it is a regular
file labelled `u:object_r:tmpfs:s0`: boot recreates that copy with identical policy bytes. Its
raw timestamp remains recorded and its complete contents and security metadata still match.

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

The init directory must be root-owned, unredirected and not group/world writable. Doctor reports
its observed UID and mode and applies the same eligibility check as the installer. Prepare an
ineligible vendor image only with a verified external recovery copy; qualification does not change
directory ownership. Grant the Next manager bootstrap root before starting its one-shot install
session.

For reinstall qualification, Doctor also recognizes a `BOOT_VERIFIED` versioned PR7 manifest and
its immutable config when both files have safe ownership and the manifest matches the running
daemon. Missing, malformed, redirected, unverified, or inconsistent markers remain blocked.
The installer separately revalidates the complete ownership inventory before an upgrade writes.

For a clean installation, pass the exact candidate APK with `--artifact`. The qualifier verifies its
clean source identity and target ABI, stages its `magiskpolicy` only after creating the challenge
backup, and executes its live-policy bootstrap from init before testing the Magisk domain. Failed
loads or changed binary bytes cannot produce the init proof. The temporary policy file is removed
with the other probes; the final backup contains none of them. Authorization must use the same APK
whose policy was qualified. Current System Mode targets may use their already qualified policy
without `--artifact`.

Automatic conversion of old init layouts, policy gzip backups, bootanim injection, and early
receipts was retired in PR7A. Remove those installations using their original manager before a
clean install. Current-format upgrades and recovery remain supported.

The host verifier requires POSIX `openat`/`O_NOFOLLOW`, process-group, and `/dev/fd` semantics.
Native reparse-point-safe Windows lifecycle integration belongs to concrete commercial-emulator
adapters.

PR8 adds explicit `--instance-kind ldplayer-v1` for the observed LDPlayer standalone-system
layout. It reads the real `vms/leidianN/leidian.vbox`, adjacent vendor configuration, private disk
layout, and controller executable. Its identity binds the VM UUID, hardware identifiers, media
registry, canonical paths, controller digest, and the corresponding ADB transport. Vendor metadata
rewrites during reboot/restore may change an inode without changing those identifiers. This does
not relax generic file identity, permit arbitrary identity-marker files, or authorize a clone.

`--instance-kind mumu-v1` covers the observed Windows Global Android 12 layout. Select its real
`vms/MuMuPlayerGlobal-12.0-N/MuMuPlayerGlobal-12.0-N.nemu`. The identity binds the VM UUID,
phone identifiers, MAC, controller, local ADB forwarding and disk attachments, including the
full SHA-256 of the shared read-only system base and private system-difference disk layout.
Changes to those fields fail qualification; routine metadata rewrites do not. Android 15 and
other backing-chain layouts are rejected. The wrapper must back up the complete private disk
set and retain/verify the immutable base; it must never overwrite another instance's shared base.

The Windows lab uses MSYS2 with an ACL-enabled private working directory and real POSIX symlink
semantics. The ordinary native Windows Python interpreter still lacks the required no-follow
APIs. Lifecycle wrappers must establish the Windows environment needed by the vendor controller,
including `PATHEXT`, and verify stopped/running postconditions rather than trusting an exit code
alone. The Windows matrix covers LDPlayer, MuMu Android 12 and BlueStacks. The exact paired
results and restored baselines are recorded in [the PR8 lab](pr8-windows-2026-10-08.md).

PR11 adds `--instance-kind mumu-macos-v1` for the observed MuMuPlayer Pro 1.9 standalone
QCOW2 layout. Select the real `bundles/mumu_N_YYYYMMDD_HHMMSS.gmad/setting/vm.json`.
The adapter binds the bundle path/index, hardware, vendor version, controller and VM engine,
and both private disks' immutable layout fields. It rejects backing chains, encryption,
internal snapshots, external data files, corrupt/unknown formats, links and shared inodes.
It permits vendor metadata rewrites, UI changes, vendor-root toggling and the observed pair
of automatic ADB ports. Rapid cycles can exceed that pair and are then refused. Use MuMu's
`config` command to assign a free fixed `customAdbPort` for repeated qualification; retain
and restore the user's original setting afterward. The fixed port is bound in the record.
Lifecycle wrappers must wait for the old VM process to exit before opening it, resolve the
new endpoint through `mumutool info`, and verify the same named bundle before connecting.
The backup still contains both complete disks with byte-verified restore; metadata tolerance
does not waive the guest inventory, three-boot or restore challenges. This is an explicit
adapter, not a relaxation of generic file identity.

Qualify upgrades and uninstall from a quiescent view of the persistent init filesystem.
Temporarily disable modules mounting `/system/etc`, cold boot, and retry a mounted-view refusal.
If an attempted operation already stopped Magisk, restart first to regain root. A disabled
provider can still rewrite metadata through its global service script: the PR11 lab uninstalled
its test providers when ReZygisk kept changing `module.prop` timestamps. Keep the byte and
metadata checks; do not reinterpret a module overlay as persistent storage. Lifecycle wrappers
should flush the guest, wait for the old process to exit, and require stable Android/ADB
readiness before returning. Retain and classify player startup errors separately from guest
and Magisk failures.

Use `--source-root /absolute/clean/candidate-worktree` on `qualify`, `doctor`, and `authorize` to
compare clean candidates with one host driver. The candidate commit must still match the APK's
embedded clean source identity. All host modules, schemas/contracts, and the artifact verifier are
independently included in the probe digest; changing the driver invalidates prior qualification.

For APK-backed qualification, PR8 checks a candidate-specific persistent-space lower bound before
writing probes. It includes the full manager APK, selected native payload, scripts and separate
rescue payload. Filesystem allocation and policy/journal overhead can need more; transactional
ENOSPC handling remains required. The factory LDPlayer test image exposed this boundary with
roughly 24 MiB free and a roughly 39 MiB debug APK.

## Create a qualification

Store the record and seal key outside the repository:

```sh
python3 tools/system_mode/kitsune.py system-mode qualify \
  --artifact /absolute/path/app-release.apk \
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

With the exact release or debug APK built from the clean qualified commit:

```sh
python3 tools/system_mode/kitsune.py system-mode authorize \
  /absolute/external/path/pr7-doctor.json \
  --artifact /absolute/path/app-release.apk \
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

The completed PR7 prepared-target record is [MuMu qualification, 2026-09-09](mumu-pr7-2026-09-09.md).
Repeat the gates for a changed runtime or target; that record is not a blanket emulator or phone claim.
