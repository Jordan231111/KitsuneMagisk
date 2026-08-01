# KitsuneMagisk current support status

Last reviewed: 2026-08-01

KitsuneMagisk remains an experimental development line. Earlier maintainer and test APKs exist,
but the resumed project has not shipped its first production release. The inherited `31.0` value is
a fork compatibility and Android upgrade-ordering value; it does not mean this branch is newer than
official Magisk.

The current hardening work is based on `kitsune` at `bcdf65f0`. Official comparison points are
Magisk v30.7 (`e8a58776`) and the observed official `master` tip `fd0cb66b`.

## What currently works

- Ordinary Magisk patch, emulator setup, manager initialization, root, and parser flows pass on the
  official Android 14, 15, and 16 ARM64 Emulator images covered by the project tests. Physical
  boot-image flashing still needs a recoverable-device qualification run.
- Direct-System/System Mode remains a separate, explicit debug-only action. It now warns before use,
  checks the target before mutation, uses a private mount namespace, and rolls back staged files on
  tested failures. It is not yet qualified as a release feature.
- Existing Kitsune/Delta HideList data is reconciled once into the active DenyList table without
  changing the rollback-compatible database version or touching SuList. Fresh installs do not run a
  legacy-data migration. See [the migration note](hide-migration-v13.md).
- Built-in update channels fail explicitly because this fork has no project-owned update service.
  An explicitly configured HTTPS custom channel must provide a SHA-256 digest; verified bytes are
  published atomically.
- Debug builds use the Android debug signer. Release builds require one private keystore/config and
  enforce manager/stub certificate identity. The historical repository test key is forbidden.
- The build and artifact gates cover required ABIs, native debug/release identity, signer separation,
  APK integrity, ELF identity, and 16 KiB ELF/ZIP alignment. Build support is not treated as runtime
  proof for every ABI.

## Exact emulator evidence

Official headless AVDs are ordinary AVDs created with `avdmanager` and launched by the same Android
Emulator used by Android Studio; `-no-window` suppresses the graphical display. The project runner
also pins its GPU, audio, snapshot, memory, image, and hardware-profile choices, so equivalence is to
that exact AVD configuration—not to every Pixel profile. These guests are suitable for repeatable
backend, boot-image, manager, root, database, package, and module tests. They do not qualify GUI or
graphics behavior, and they do not reproduce a physical device's bootloader, vendor kernel,
partitions, recovery, or firmware.

The one tested BlueStacks target is the existing Air 5.21.782.7501 `Tiramisu64` instance. Its normal
root/backend adapter and read-only-System rejection were exercised without cloning the instance.
Host logs also reproduced BlueStacks process/ADB/storage-startup failures with an unchanged known-good
payload, so that observed intermittent case is strongly vendor-side. This does not prove that every
future boot failure is unrelated to Magisk or installed modules.

Support remains attached to an exact emulator/device version, Android image, ABI, page size,
filesystem/layout, bootstrap method, and tested lifecycle. The detailed evidence ledger is in
[`DEVELOPMENT_ROADMAP.md`](../DEVELOPMENT_ROADMAP.md).

## Release blockers

1. Finish manifest-owned, crash-recoverable System Mode install, upgrade, addon, uninstall, and
   restore transactions.
2. Qualify System Mode on an exact writable, snapshot-capable target through install, three cold
   boots, upgrade/reinstall, root/module checks, uninstall, and verified restoration.
3. Create a protected production signing identity and an explicit transition from APKs signed with
   the historical public test certificate.
4. Establish a project-owned authenticated update service before enabling built-in update channels.
5. Forward-port the tested Kitsune behavior onto an audited current official stable core; do not
   independently merge hundreds of official `master` commits into this old core.
6. Select and qualify a Zygisk architecture and define HideList/SuList/provider behavior by exact
   provider version. ReZygisk 1.0.0 must not be advertised as Kitsune-compatible.
7. Complete physical-device, commercial-emulator, ABI/runtime, recovery, multiuser, SELinux,
   hidden-manager, safe-mode, and failure-injection matrices for every support claim.
8. Resolve or explicitly carry remaining dependency/security holds, including the RSA timing
   advisory with no fixed upstream version.

The ordered implementation plan and acceptance criteria are maintained in the
[development roadmap](../DEVELOPMENT_ROADMAP.md). Historical purpose and divergence conclusions are
summarized in the [faithfulness audit](faithfulness-audit.md).
