# Installing KitsuneMagisk

Download **app-release.apk** from the [KitsuneMagisk releases](https://github.com/Jordan231111/KitsuneMagisk/releases).
The current release is **31023**. Installing the APK installs the manager; it does not root the device.

The [compatibility table](../compatibility/TESTED.md) lists tested targets.
Physical-phone qualification remains [open](https://github.com/Jordan231111/KitsuneMagisk/issues/39).

## Updating an existing installation

For an ordinary boot-image installation already using this Kitsune line, open the app,
choose **Install → Direct Install**, wait for completion and restart the phone.

Switching from official Magisk, an older Kitsune package or a debug APK is a separate migration.
Follow the [package and signing transition](build-info.md#pr15-namespace-migration);
do not treat another root implementation as an ordinary in-place update.

## First installation on a phone

1. Back up your data. Check that your exact model supports bootloader unlocking and follow its
   manufacturer's procedure. Unlocking normally erases the phone.
2. Obtain the original firmware matching your model and installed software build. Keep an
   untouched copy for recovery. Confirm which image your device uses: usually `init_boot.img`
   on newer devices, otherwise `boot.img`. Devices without a boot ramdisk may use recovery.
3. Install this project's APK on the phone and copy the original image to it.
4. In the app, choose **Install → Select and Patch a File**. Select the image, wait for completion
   and copy `magisk_patched_….img` from Download back to your computer.
5. Flash that patched image to the matching partition using your model's flashing instructions.
   Start the phone, open KitsuneMagisk and complete any additional setup it requests.

Patch the image on the device where it will be used. Do not flash another person's patched image
or guess the partition. The
[upstream installation guide](https://topjohnwu.github.io/Magisk/install.html)
explains fastboot, recovery and boot-layout differences; use the Kitsune APK for this fork.

### Samsung phones

Samsung uses an **AP firmware archive** and its own flashing procedure.
Follow the [Samsung instructions](https://topjohnwu.github.io/Magisk/install.html#samsung-devices)
with this project's APK. First installation wipes data and permanently trips Knox.
Do not apply the generic fastboot procedure or restore individual stock partitions afterward.

### Custom recovery

Renaming the APK to a ZIP remains an inherited installation route, but it is deprecated upstream
and is not qualified on physical devices in this fork. Prefer patching the correct firmware image.
See [upstream recovery instructions](https://topjohnwu.github.io/Magisk/install.html#custom-recovery)
for its restrictions.

## Emulators

System Mode is available in both release and debug builds from app version code **31023**.
Older Beta 4/code 31021 release APKs still lack this feature.

The writable-emulator route requires bootstrap root and the
[host qualification procedure](system-mode/pr7-machine-qualification.md), including a verified
external recovery backup. It currently requires technical preparation.

For MuMu, fully close and reopen the selected instance using its CLI, then reconnect ADB.
Do not use `adb reboot`. Phone fastboot instructions do not apply to MuMu.
Android Studio AVD testing uses the separate boot-image route described in
[device testing](device-qualification.md).

## Uninstalling

Use **Uninstall** in the app and wait for completion before restarting. If System Mode reports
that a module is still in use, disable that module and cold-start the emulator before retrying.
Managed System Mode keeps user modules, policies and scripts; its recovery and external-restore
procedures are in the qualification guide. Legacy installations require their matching legacy
uninstaller or recovery procedure.
