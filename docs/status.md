# KitsuneMagisk current support status

Last reviewed: 2026-07-22

Reference branch before PR3/PR4 work: `kitsune` at `cf149fcf`

## Release level

KitsuneMagisk is in active experimental development. Existing `v31.0-*` artifacts are not qualified
stable System Mode releases. The `31.0` label was inherited as a module-compatibility and Android
upgrade-ordering value; it does not describe the age of the underlying Magisk source.

## Evidence currently available

| Area | Current evidence | Status |
|---|---|---|
| Repository build | PR3/PR4 source passes host/JVM tests, zero-error full Android lint, canonical debug/release builds, and Gradle debug native links for ARM64, ARMv7, x86, and x86_64. The macOS paths also disable the pinned ONDK output-sync defect and explicitly retain section GC so Gradle debug cannot pull dead ARMv7 unwind code. | Buildable |
| Normal Magisk AVD flow | Current CI covers API 23, 29, and 35 x86_64 jobs; the final disposable API 35 Google APIs ARM64 run completed debug and release ramdisk patch, manager setup, reboot, app self-test, and `su -c id`, then byte-restored the stock image and deleted the AVD. See the [PR4A lab record](system-mode/avd-lab-2026-07-22.md). | Tested on x86_64 CI and local ARM64 |
| Normal real-device install | Static tracing confirms the non-emulator Direct Install route still discovers, patches, and flashes the boot-family image; Direct-System remains a separate explicit action. No physical device was flashed in this audit. See the [faithfulness audit](faithfulness-audit.md). | Preserved in source; runtime qualification open |
| System Mode contract | Versioned doctor, reason codes, install manifest/state-machine schemas, fixtures, failure-injection plan, and ADB driver are implemented; three sanitized real records cover MuMu plus immutable API 35 16 KiB/API 36 ARM64 guests | Source-ready; lifecycle qualification incomplete |
| Direct-System/System Mode | Implementation and historical LDPlayer/MuMu/Nox-specific fixes exist, but no dedicated CI job runs `direct_install_system` | Experimental |
| LDPlayer | Individual configurations may work; no exact current version/image has completed the new qualification gate | Unqualified |
| MuMuPlayer 12 | A read-only record from engine 1.4.46 on port 16384 found writable ext4 and a pre-existing legacy install; the instance was not clean and no cold boot or restore was run | Characterized, not qualified |
| Immutable Android Studio images | Fresh API 35 16 KiB and Android 16/API 36 ARM64 guests exposed EROFS, enforcing AVB/dm-verity, and no bootstrap root; the doctor rejected both without mutation | Correctly blocked negative evidence |
| NoxPlayer | A historical Android 12 `/sbin` regression was fixed; current exact images still require repeatable qualification | Experimental evidence only |
| BlueStacks | ADB availability alone does not provide a supported writable-system/bootstrap path | Unsupported until an adapter is proven |
| Real devices | Requires a recoverable writable layout and explicit AVB/filesystem qualification | No general support claim |
| DenyList | Fresh/reselected normal-hide entries use `denylist` and can interoperate with compatible external providers | Works with upgrade caveat |
| Existing HideList data | Current source adds transactional v13 union migration, a verified version-12 DB backup, an audit row, and divergent/SuList fixtures | Implemented in source; published older artifacts remain unsafe |
| App/stub updater | Built-in inherited channels make no metadata request and show an explicit project-service-unavailable result; only an explicitly configured HTTPS custom channel can be queried | Contained pending PR10 service |
| SuList/external Zygisk | Current provider projects do not establish one universal SuList contract | Version-specific/unsupported unless tested |

## Current release blockers

1. System Mode has no maintained install/cold-boot/upgrade/uninstall/rollback matrix.
2. Recovery System Mode still reaches boot-image discovery before its System Mode branch.
3. Installer selection depends on filename magic in one recovery path.
4. The SELinux capability probe changes the live policy instead of only inspecting capability.
5. Installation and uninstall are not yet manifest-owned and transactional.
6. Project-owned, digest-validated app/stub update infrastructure does not yet exist; current source
   disables inherited endpoints and reports the failure explicitly.
7. The Rust dependency audit still has an unfixed RSA timing advisory plus five informational
   warnings. Current RSA private-key signing is a local maintainer operation rather than a network
   oracle, but the finding and the isolated `rand`/vendored-`cxx` updates remain pre-stable work.
8. Built-in Zygisk was removed from the current fork, and package signature enforcement remains
   globally disabled. The forward port must retain official built-in Zygisk through parity and
   restore upstream signature trust unless a narrow, threat-modeled hidden-manager test proves a
   required exception.

## What “supported” will mean

Support will be attached to an exact emulator/device version, Android image, ABI, filesystem/layout,
bootstrap method, and tested lifecycle. A supported target must pass installation, three cold boots,
root/module smoke, upgrade/reinstall, uninstall, and snapshot or stock restoration. Unsupported
immutable layouts must be rejected before persistent mutation.

Until those records exist, use disposable emulator instances or recoverable snapshots and describe
successful configurations as test evidence rather than general product support.

## Development direction

Current `kitsune` remains the comparison baseline. After rechecking official releases at branch cut,
`next-system` will forward-port System Mode onto the latest audited stable (currently Magisk v30.7)
and will replace `kitsune` only after identical parity tests pass. See
[`DEVELOPMENT_ROADMAP.md`](../DEVELOPMENT_ROADMAP.md) for the implementation sequence and complete
acceptance criteria.
