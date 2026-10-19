# Device testing

The [compatibility table](../compatibility/TESTED.md) records exact tested targets and APKs.
The [PR16 release record](release-pr16.json) identifies Beta 4; the
[PR15 requirement audit](device-pr15/requirements-audit.md) covers the preceding implementation.
These are experimental results. Physical-phone qualification remains
[open](https://github.com/Jordan231111/KitsuneMagisk/issues/39).

## What a virtual device proves

An AVD profile changes simulated hardware settings. A profile named Pixel or Samsung still uses
an emulator kernel and SDK image; it does not reproduce that phone's bootloader, partition layout,
firmware, encryption or recovery. See [Android's AVD model](https://developer.android.com/studio/run/managing-avds)
and [Cuttlefish](https://source.android.com/docs/devices/cuttlefish).

Record the exact source, APK hash, image, ABI, page size, SELinux state and distinct cold-boot IDs.
Builds, synthetic image fixtures and repeated observations from one boot cannot replace runtime
qualification. Module installation alone does not prove hiding, audio, charging or attestation;
those results belong in the [module matrix](module-compatibility.md).

## Reproducing the checks

Use `scripts/env.py` for standalone tools. `scripts/avd.sh test API TYPE` patches a copy of the
SDK ramdisk and runs debug/release setup, SU, modules, Hide/SuList, providers, manager migration
and recovery. It closes and restarts the owned emulator process without overwriting the SDK ramdisk.

Configure each disposable instance with:

- `MAGISK_AVD_DEVICE`: installed hardware profile.
- `MAGISK_AVD_NAME` and `MAGISK_AVD_PORT`: unique instance name and port.
- `AVD_QUALIFICATION_DIR`: output directory for guest and boot records.
- `AVD_BOOT_FIXTURES=1`: MagiskBoot fixture tests.
- `AVD_STRESS_ITERATIONS=8` and `AVD_STRESS_PARALLEL=6`: 48 concurrent-root test requests per variant.
- `AVD_DISABLE_STORE_UPDATES=1`: optional disposable Play Store image control, recorded in
  `store-updater-state.txt`. This prevents guest updates from interrupting tests, not production updates.

`scripts/env.py python3 tools/boot_qualification.py --serial SERIAL --apk APK --output REPORT`
checks boot headers v0–v4, init_boot/vendor_boot, original-init restoration, kernel preservation,
Vivo/Samsung signatures, compression and AVB1 signatures against independent tools.
It does not boot an OEM kernel. Temporary signing keys and guest fixtures must be removed.

`scripts/env.py python3 tools/device_qualification.py render --check` verifies the generated
compatibility outputs against `docs/device-pr15.json`. Missing evidence, reused boot IDs, dirty
sources and unsupported status changes fail validation.

### MuMu

Use `mumutool`, Android Device-1 and an explicit ADB serial. Back up both stopped-instance disks,
verify their restore bytes, and cold start by fully closing and reopening the instance.
Do not use `adb reboot`. Restore the user's original instance after testing.
[System Mode qualification](system-mode/pr7-machine-qualification.md) is required before installation.
The release APK still blocks System Mode; [#47](https://github.com/Jordan231111/KitsuneMagisk/issues/47)
tracks that delivery gap.

### BlueStacks and Waydroid

The [BlueStacks lifecycle](device-pr15/bluestacks-lifecycle.json) uses a disposable instance and
an external bootstrap tool, which is not redistributed. Its experimental host-initramfs adapter
(`tools/system_mode/host_image.py`) verifies APK/ARM payloads and preserves unrelated archive
entries. Stop players before publishing an image and retain a verified factory restore.
This route begins at post-fs-data; it does not qualify pre-init mounting, Direct-System,
inactive-slot OTA or recovery installation.

The [Waydroid lifecycle](device-pr15/waydroid-lifecycle.json) covers one Android 13/Waydroid 1.6.2
tuple with a lab init fixture and SELinux absent. It does not qualify a production installer or
other kernels. Neither result is a vendor-wide support claim.

## Compatibility changes and remaining boundaries

The implementation records retain the fixes and their exact test inputs:

| Area | Record |
| --- | --- |
| Native child exit status | [Regression and controls](device-pr15/child-exit-status.json) |
| Long/read-only properties and notifications | [Property overrides](device-pr15/property-overrides.json), [resetprop comparison](device-pr15/resetprop-comparison.json) |
| Bootstrap root and detached setup completion | [Live setup](device-pr15/live-setup.json) |
| Early mounts, policy parsing and module partitions | [Module contract](module-contract.md), [PR14 checks](module-pr14.json) |
| Hidden-manager registration and SU consent | [Manager contract](manager-contract.md), [CI acceptance](device-pr15/ci-acceptance.json) |
| Upstream fixes and Android-compatible AVB1 signing | [Applied changes](device-pr15/upstream-fixes.json) |
| Module and third-party hiding limits | [Compatibility matrix](module-compatibility.md), [signing diagnosis](device-pr15/shamiko-signing-identity.json) |

Managed System Mode requires Android 7.1/API 25 or later and live target/recovery authorization.
Recovery-ZIP `SYSTEMMODE` and automatic managed `addon.d` replay are unsupported: recovery lacks
the authorization, and an OTA changes the bound image. Ordinary Magisk recovery and `addon.d`
remain separate routes. A changed writable image must be restored or requalified before reinstalling.

Physical GrapheneOS (`ZYG-003-device`), legacy Sony (`BOOT-006`), A/B OTA (`ORD-003`) and recovery
ZIP/stock restoration (`ORD-004`) remain unqualified. The [feature ledger](port-ledger.md) preserves
their implementation and blocks stable promotion. A Mac host reboot and commercial-emulator
version upgrade also remain unqualified.

## CI controls and unresolved failures

[CI acceptance](device-pr15/ci-acceptance.json) retains failed attempts as well as passing repeats.
The native-crash gate remains active; an unchanged-artifact retry does not prove a failure fixed.
Unexplained virtual-device failures are tracked in
[#43](https://github.com/Jordan231111/KitsuneMagisk/issues/43), and System Mode qualification drift in
[#46](https://github.com/Jordan231111/KitsuneMagisk/issues/46).

[SDK boot controls](system-mode/sdk-native-boot-controls.md) define the narrowly matched stock
crash exceptions. The [Android 9 fixture](device-pr15/ci-api28-composer-fixture.json) changes a
copied vendor init rule to preserve composer restart ordering. Its passing results apply to that
modified lab image; the unmodified image remains unqualified. Android 6 controls verify persisted
setup state before cold boots and framework restarts. Root/unroot transitions verify the observed
UID after reconnecting. No test timeout or crash gate is relaxed to obtain a pass.

Remove disposable instances, probes, backups and build outputs after verification. Preserve shared
SDK inputs, unrelated devices and permanent signing material. Historical [PR15 cleanup](device-pr15/cleanup.json)
and current [PR16 cleanup](release-pr16.json) identify what was restored and removed.
