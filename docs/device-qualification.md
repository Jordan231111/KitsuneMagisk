# PR15 qualification contract

PR15 uses the available Android Studio AVDs, MuMuPlayer Pro **Android Device-1**, and a newly
created BlueStacks Air instance. LDPlayer and Nox are outside this run's requested scope.
Physical phones are unavailable. The [generated table](../compatibility/TESTED.md) reports exact
observations; it does not confer production status or physical-device support.

## What a virtual device proves

An AVD hardware profile changes the simulated screen, memory and peripherals. It still boots an
emulator kernel and SDK system image. Renaming it Pixel, Samsung or RedMagic does not simulate
the named phone's bootloader, device tree, partition layout, firmware, encryption or recovery.
An OEM kernel release alone cannot supply those missing components or make that kernel boot on
the emulator's virtual hardware. Custom AOSP kernels/images must target the emulator or Cuttlefish.
See [Android's AVD model](https://developer.android.com/studio/run/managing-avds) and
[Cuttlefish](https://source.android.com/docs/devices/cuttlefish).

The matrix therefore separates real emulator boots, synthetic boot-image fixtures, unsupported
installation routes and unqualified physical targets. Two matching boot IDs cannot count as two
cold boots. Build-only ABIs, permissive SELinux and fixture headers cannot substitute for matching
runtime evidence. Each accepted run names the exact APK digest and clean source identity.

The [module compatibility matrix](module-compatibility.md) separately records pinned third-party
ZIPs, ordinary lifecycle checks, selected functional probes and official Magisk controls. A successful
installation is not proof of working hiding, audio processing, charging control or hardware attestation.

## Reproducing the checks

Use the repository environment wrapper and a disposable SDK device. `scripts/avd.sh test API TYPE`
patches a copy of the SDK ramdisk, runs debug and release setup, closes and restarts the owned
emulator process, and exercises SU, modules, Hide/SuList, provider, manager migration and recovery.
`MAGISK_AVD_DEVICE` selects an installed hardware profile; `MAGISK_AVD_NAME` and
`MAGISK_AVD_PORT` isolate the instance. `AVD_QUALIFICATION_DIR` retains exact guest/boot records.
Set `AVD_BOOT_FIXTURES=1` to run the APK's MagiskBoot tests. No shared SDK ramdisk is overwritten.
For disposable Play Store images, `AVD_DISABLE_STORE_UPDATES=1` disables the Store updater
and records that choice in `store-updater-state.txt`. A Play Services update terminated a
running hidden-manager suite through Android's package-dependency handling. Pinning the guest
software prevents that environmental change during acceptance; it is not a crash exemption
or an instruction to disable updates on users' devices. Those temporary AVDs are deleted afterward.

`scripts/env.py python3 tools/boot_qualification.py --serial SERIAL --apk APK --output REPORT`
tests boot headers v0–v4, init_boot, vendor_boot v3/v4, original-init restoration, unchanged kernels,
Vivo and Samsung patch signatures, and compressed-kernel `split -n`. OpenSSL independently signs
and verifies RSA/P-256/P-384/P-521 AVB1 records; tampered and nonstandard raw EC signatures fail.
Temporary private keys and guest fixture files are removed. These fixtures do not boot OEM kernels.
The expanded corpus also rejects truncated gzip/XZ/bzip2 inputs, removes partial outputs,
preserves empty streams, Samsung signing trailers and AVB2 descriptors, and checks ARM32 zImage
payload relocation against an independent gzip decoder and the exact header pointers.

`scripts/env.py python3 tools/device_qualification.py render` generates both public compatibility
outputs from `docs/device-pr15.json`. CI checks for stale output and rejects unsupported status
promotion, missing evidence, reused boot IDs, dirty source and incomplete acceptance records.

MuMu lifecycle operations use its `mumutool` CLI and an explicitly pinned ADB serial. Save both
stopped-instance disks, verify restore bytes, and cold start by closing and reopening the player.
Do not use `adb reboot`. Preserve the user's original instance and restore it after testing.

BlueStacks bootstrap uses the separately supplied `blueStackRoot.command` on the new instance.
Its legacy root and shared host-initramfs hook are bootstrap evidence, not a claim that the current
Kitsune Direct-System transaction supports BlueStacks. A read-only system remains a refusal even
after bootstrap root succeeds. Live setup is temporary and must be identified separately.

The experimental host-initramfs route stages an offline copy with
`tools/system_mode/host_image.py`. It replaces only the owned payload and bootstrap script,
checks the APK identity and ARM binaries, and preserves unrelated archive entries. Its controller
must select the disposable instance, flush guest writes before closing it, stop players before
publishing the image, verify the result and retain a verified factory restore. Root/unroot use the
existing per-instance RootMac gate. The supplied tool is external and is not redistributed here.
This route starts at post-fs-data; it does not provide magiskinit pre-init mounting or qualify the
ordinary in-app Direct Install, inactive-slot, OTA or recovery routes on BlueStacks.

## Compatibility changes and remaining boundaries

Candidate `31017` adds the [Waydroid detection and companion fixes](device-pr15/waydroid-diagnosis.json).
Its consistent signed build and runtime qualification are pending. Existing `31016` records below
remain evidence for their exact artifacts, not automatic acceptance of the new candidate.

- PR15 retains the v31.0 baseline plus [verified fixes](device-pr15/upstream-fixes.json), as selected
  by the user. It preserves interactive SU consent and Kitsune Hide/SuList. Candidate `31016`
  corrects primary-group precedence, compressed-image error handling, Samsung trailer preservation,
  AVB2 descriptor preservation, ARM32 payload detection/relocation and module filename controls.
  Unreleased upstream feature and UI rewrites are outside this qualification candidate.
- Updating an existing `ro.appcompat_override.*` property now uses the existing recreation path,
  which translates its name into the override tree. The inherited direct update dereferenced a
  missing prefixed entry; both candidate `31015` and official Magisk 30.7 reproduced the crash.
  [Paired controls](device-pr15/property-overrides.json) and `tools/property_qualification.py`
  check short/long/empty transitions and ordinary updates with independent platform readers.
  The fixture uses copied property areas in a private mount namespace and verifies the original
  namespace afterward. Normal property notifications retain the v31 behavior documented in the
  [serial/waiter comparison](device-pr15/resetprop-comparison.json).
- The maintained manager now uses `io.github.jordan231111.magisk`; the home page retains its
  existing layout and contributors, with Jordan231111 as the fork owner's name and GitHub link.
  [Namespace migration](build-info.md#pr15-namespace-migration) requires an explicit installation
  transition. MuMu also required its own per-app bootstrap-root grant for the newly installed package.
  This run required a full CLI close/open after saving the grant to refresh MuMu's policy service.
  The same-package `31011` to `31012` System Mode upgrade preserved the test sentinel and module whiteout.
- Concurrent update clients can share a newly created state directory. Losing the `mkdirs()` race
  no longer reports a false storage error; the existing `lstat` check still rejects redirected paths.
  Android instrumentation exercises repeated concurrent first-use construction.
- Pre-init budget parsing uses its own field separator. ViPER's installer leaves `IFS` set to
  newline; inherited parsing incorrectly treated its 790-byte policy and entry count as one
  integer, then triggered the existing safety fallback. The new parser accepts that valid input
  while retaining the same size/count limits and rejecting an oversized sparse policy. Candidate
  `31013` passes the paired ViPER AVD check and the [MuMu lifecycle](device-pr15/mumu-31013-lifecycle.json),
  including removal and original-image restoration. Earlier records retain their exact artifact IDs.
- Manager package lookup tolerates unrelated directories removed during Android package migration.
  The previous traversal could abort during original-manager removal and clear the hidden manager's
  registration. A failed scan now preserves that registration without authorizing an unverified app;
  the certificate and dynamic-APK checks remain required. The host regression reproduces the old
  `ENOENT` failure and checks the repaired lookup, package-prefix bounds and other I/O errors.
- AVB1 ECDSA now uses Android's DER encoding, following
  [upstream 87e72a582](https://github.com/topjohnwu/Magisk/commit/87e72a582e019ae1bb3e65c90c259e994ecd66fa).
  Earlier raw `r || s` EC signatures were not Android-compatible. Repatch and re-sign the matching
  original image; do not treat an old MagiskBoot self-verification result as bootloader acceptance.
  The image algorithm now comes from its signing key rather than the certificate issuer's digest.
  Mismatched keys/certificates and unsupported signature versions or algorithms fail before writing.
- Restore the exact historical Vivo `do_mount_check` encoded `/system` replacement. Matching bytes
  are required; this is not a manufacturer-name bypass. Samsung PROCA remains an independent patch.
- Live setup discovers bootstrap `su` through PATH and uses its command interface. A missing
  `/system/xbin/su` must not prevent a rooted emulator with `/system/bin/su` from being tested.
  An elevated worker reports completion independently of the bootstrap SU connection, since
  replacing that daemon can close its own caller's socket. Bootstrap denial and worker failure
  remain failures; an overdue worker retains its diagnostic directory. The
  [live-setup record](device-pr15/live-setup.json) covers stock cold boot, reinjection, the full
  release suite and an ordinary-shell PATH bootstrap followed by the API v5/root stress checks.
- Select-and-Patch on managed System Mode uses a separate workspace and cleans it after success
  or failure. A subsequent boot must not import file-patching tools over the transaction-owned
  runtime. The daemon also preserves that runtime when an old ordinary update inbox remains.
  A previously damaged installation still requires its verified recovery/restore path.
- System Mode snapshots preserve module whiteouts (character device `0:0`) and module symlinks.
  The same rules cover restore and rollback cleanup. FIFOs, sockets, other device nodes and
  whiteouts outside module content remain unsupported; they are never opened to calculate a hash.
- SU consent launches allow up to fifteen seconds per framework-start attempt, within the existing
  seventy-second request deadline. A launch never grants consent; stalled children are killed and
  reaped, and the authenticated response is still required. Cuttlefish exposed the earlier three-second
  cutoff killing ART before ActivityManager received the request.
- Normal zygote SELinux-domain authentication remains authoritative. On a permissive image whose
  init and peer both remain in the kernel domain, the fallback requires UID 0, four root UID fields,
  a system `app_process` image and verified init-owned zygote ancestry. Pre-specialization fork and
  USAP children must run the same image as their root zygote parent. Non-root peers, wrong images,
  unrelated ancestry, enforcing mode and unreadable/malformed state fail. BlueStacks exercises this
  unusual **permissive** case; it does not prove SELinux-disabled or Waydroid coverage.
- Managed Direct-System remains an explicit, authorized debug capability. Recovery ZIP
  `SYSTEMMODE` and automatic System Mode `addon.d` replay are not supported: recovery lacks the
  live target/host recovery authorization, and an OTA changes the bound image. Use normal image
  patching where available, or restore/requalify the changed writable image before reinstalling.
  Ordinary Magisk recovery and ordinary `addon.d` remain separate inherited routes.
- Retire the historical Android 6/API 23–24 System Mode availability claim. The maintained
  transaction requires API 25 or later and the exact target qualification; ordinary Android 6
  boot-image installation remains covered by its separate CI lanes. Legacy System Mode users
  need the matching legacy uninstall/recovery procedure and an ordinary installation on that OS.
- Physical OEM boot, encrypted pre-init storage, A/B OTA, hardware recovery and GrapheneOS device
  claims require actual matching devices. No available emulator result clears these release gates.
  Retain the implementation and existing historical line; do not remove useful compatibility
  code merely because hardware is unavailable.

The six unavailable target rows are explicit PR16 release blockers: GrapheneOS (`ZYG-003-device`),
legacy Sony (`BOOT-006`), physical A/B OTA (`ORD-003`), recovery ZIP/stock restoration (`ORD-004`),
and SELinux-disabled/container Hide and SU lifecycles (`HID-014-device`, `SU-004-device`).
Their `unqualified-target` ledger state requires a named target, reason and evidence, preserves
the implementation, and always fails the production release audit. It is separate from an
implementation deferral, which still cannot be moved past PR15. This keeps the requested
available-lab work and the missing device/ROM evidence explicit.

## Retained CI investigation

[Run 39072209066](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39072209066)
also passes all 33 jobs on its first attempt at PR head `82142208f`. The earlier
startup failures below remain part of the record; a successful repeat does not erase them.

[Run 39064935566](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39064935566)
finishes with all 33 jobs passing. The [acceptance record](device-pr15/ci-acceptance.json)
includes the exact merge-source identity, Cuttlefish records and both Android 6 architectures.
Android 6 passes four stock cold boots and four framework restarts after the setup persistence
check described below. API 30/x86_64 needed one retry: its first release boot had an `audioserver`
SIGSEGV in `AudioPolicyService::AudioCommandThread::changeAudioVolumeGroupCommand` before the
app tests. The same artifact passed the job on retry. That crash is retained as unexplained,
not described as fixed or exempted; the native-crash gate still fails a recurrence.

[Run 39015915833](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39015915833)
timed out in Cuttlefish's hidden-manager application-PATH root check after the native SU request
was granted. The last child diagnostic was namespace setup; the existing logs do not identify
which later operation stopped progressing. This is not claimed as a fixed runtime bug or dismissed
as an emulator fault. Two complete subsequent Cuttlefish attempts in
[run 39019753847](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39019753847) passed,
including normal/hidden manager checks and Direct Install. Their exact boot records are linked
from the compatibility table. Cuttlefish timeouts now collect bounded native worker stacks and
syscall state before teardown, without reading process environments or arguments. A recurrence
still fails the test; no timeout or acceptance condition was relaxed.

[Run 39024117895](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39024117895) passed
Cuttlefish but exposed the package-removal race on API 36.1. API 29/x86 also failed to observe a
consent dialog; its next run retains the window hierarchy on failure. API 23/x86_64 completed the
app suites but failed the native-crash gate on a SurfaceFlinger `eglMakeCurrent` crash. Stock
framework-restart controls now include that image; the crash has not been exempted. A local ARM64
API 36.1 run passed hidden-manager tests, then the restored manager's final test process crashed;
that attempt remains failed. Corrected emulator log filtering and bounded failure log capture
retain evidence for the next run. The final acceptance records identify candidate `31016`.

The Android 6 x86/x86_64 stock controls in
[run 39048129575](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39048129575) and
[run 39051364678](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39051364678)
failed a framework-only stop/start before any Magisk payload was installed. Waiting for init
to reap the old services fixed the observed shutdown/start race, but the restarted stock
framework still did not complete boot on emulator 37.3.4. Those targets retain four stock
**cold-boot** controls and the full patched-image suites; that warm-restart configuration is unqualified.
The native-crash gate on tested Magisk boots remains unchanged. API 29/x86's consent failure
in the first run was obscured by a Quickstep ANR dialog; subsequent failures also retain Android's
ANR stacks rather than dismissing the dialog. API 27/x86 exhausted that run's time limit after
a slow SDK image download, so downloads now have a separate bounded step from device tests.

The latter run's API 25/x86 timeout was a guest-kernel panic in
`goldfish_pipe_dma_clear_lock`, called from `goldfish_interrupt_task`; all ten consent requests
had been denied before the guest stopped responding. The legacy CI lanes now disable the
emulator's optional `GLDMA`/`GLDMA2` features, using its ordinary graphics-copy path. This is
an emulator configuration, not a Magisk or phone-kernel patch. API 24/25 x86 and x86_64 passed
their full suites in [run 39055905296](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39055905296).
The API 23 stock warm restart still timed out with DMA disabled at that stage; the later
setup persistence check restores the cold-boot and framework-restart coverage. The
[emulator feature mapping](https://android.googlesource.com/platform/external/qemu/+/emu-master-dev/android/android-emu/android/opengles.cpp)
and [Goldfish driver](https://android.googlesource.com/kernel/goldfish-modules/+/ca47ac740d16ecf731be3fecdd051d26932f9bdd/goldfish_pipe_v2.c)
describe the affected transport.

[Run 39059527601](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39059527601)
also timed out on the second **stock cold boot** of both API 23 architectures, before APK
installation. The guest reached ActivityManager startup but did not finish boot; its log
advertised OpenGL ES 3.0. Those two CI lanes now explicitly disable `GLESDynamicVersion`
to request the legacy ES 2 path described in the emulator's feature configuration and
[release notes](https://developer.android.com/studio/releases/emulator#26.1.3).
Four stock cold boots and all patched-image tests remain required; this configuration
change is not itself a passing qualification result.

The [GLES 2 control](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39062154801)
confirmed `qemu.opengles.version=131072` but still stalled before Magisk installation.
A comparison with official emulator **35.3.11 / build 12836668** was queued using
[Android's checksum-pinned archive](https://developer.android.com/studio/emulator_archive).
It could not start on the runner because `libpulse.so.0` was absent, so that attempt supplies
no guest qualification and the temporary runtime selection was removed.
The logs then identified a setup persistence race: SdkSetup disabled itself at 19:11:34.822,
but the VM closed at 19:11:41.551, before Android 6's ten-second PackageManager write delay.
The next boot launched SdkSetup again and stalled during its self-disable sequence.
The stock-control harness now verifies that the disabled component is present in the on-disk
package restrictions before shutdown or framework restart. This follows the
[setup component](https://android.googlesource.com/platform/development/+/android-6.0.1_r81/apps/SdkSetup/src/com/android/sdksetup/DefaultActivity.java)
and [PackageManager write schedule](https://android.googlesource.com/platform/frameworks/base/+/android-6.0.1_r81/services/core/java/com/android/server/pm/PackageManagerService.java).
The current emulator is used again with four cold boots and four stock framework restarts.
Both architectures passed the persisted-state check and full patched-image suites in the
acceptance run above; no crash is exempted.

[Run 39055905296](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/39055905296)'s
Android 17/API 37.0 16 KiB SDK image completed the app suites but failed the native-crash
gate: an `app_process` consent launcher aborted during ART `DestroyJavaVM` with an invalid
`pthread_join` handle. This is not attributed to the emulator without a matching control.
The API 37 lanes now run fifty stock `app_process` AM startup/shutdown checks before installing
Magisk and retain those results. Their native-crash gate remains active.
