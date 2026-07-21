# KitsuneMagisk current support status

Last reviewed: 2026-07-21  
Reference branch: `kitsune` at `25fa2159`

## Release level

KitsuneMagisk is in active experimental development. Existing `v31.0-*` artifacts are not qualified
stable System Mode releases. The `31.0` label was inherited as a module-compatibility and Android
upgrade-ordering value; it does not describe the age of the underlying Magisk source.

## Evidence currently available

| Area | Current evidence | Status |
|---|---|---|
| Repository build | Debug and release variants build in the existing GitHub workflow | Buildable |
| Normal Magisk AVD flow | API 23, 29, and 35 jobs patch an AVD ramdisk, boot, initialize the manager, and verify `su -c id` | Tested by current CI |
| Direct-System/System Mode | Implementation and historical LDPlayer/MuMu/Nox-specific fixes exist, but no dedicated CI job runs `direct_install_system` | Experimental |
| LDPlayer | Individual configurations may work; no exact current version/image has completed the new qualification gate | Unqualified |
| MuMuPlayer 12 | Individual configurations may work; persistent writability and cold-boot evidence are not yet published | Unqualified |
| NoxPlayer | A historical Android 12 `/sbin` regression was fixed; current exact images still require repeatable qualification | Experimental evidence only |
| BlueStacks | ADB availability alone does not provide a supported writable-system/bootstrap path | Unsupported until an adapter is proven |
| Real devices | Requires a recoverable writable layout and explicit AVB/filesystem qualification | No general support claim |
| DenyList | Fresh/reselected normal-hide entries use `denylist` and can interoperate with compatible external providers | Works with upgrade caveat |
| Existing HideList data | No schema migration from existing `hidelist` rows was added with `25fa2159` | Migration required |
| SuList/external Zygisk | Current provider projects do not establish one universal SuList contract | Version-specific/unsupported unless tested |

## Current release blockers

1. System Mode has no maintained install/cold-boot/upgrade/uninstall/rollback matrix.
2. Recovery System Mode still reaches boot-image discovery before its System Mode branch.
3. Installer selection depends on filename magic in one recovery path.
4. The SELinux capability probe changes the live policy instead of only inspecting capability.
5. Installation and uninstall are not yet manifest-owned and transactional.
6. Existing `hidelist` database rows are not migrated to the canonical `denylist` table.
7. In-app update and stub download endpoints inherited from prior maintainers currently fail.

## What “supported” will mean

Support will be attached to an exact emulator/device version, Android image, ABI, filesystem/layout,
bootstrap method, and tested lifecycle. A supported target must pass installation, three cold boots,
root/module smoke, upgrade/reinstall, uninstall, and snapshot or stock restoration. Unsupported
immutable layouts must be rejected before persistent mutation.

Until those records exist, use disposable emulator instances or recoverable snapshots and describe
successful configurations as test evidence rather than general product support.

## Development direction

Current `kitsune` remains the comparison baseline. `next-system` will forward-port System Mode onto
official Magisk v30.7 and will replace `kitsune` only after identical parity tests pass. See
[`DEVELOPMENT_ROADMAP.md`](../DEVELOPMENT_ROADMAP.md) for the implementation sequence and complete
acceptance criteria.
