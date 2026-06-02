# Kitsune Magisk — maintenance notes

A maintenance continuation of **Kitsune Magisk** (Magisk Delta lineage), based on the
`VisionR1/KitsuneMagisk` snapshot (version **31000 / 31.0-kitsune**), with the fixes needed to
build and ship via CI again after upstream development stalled.

## Fixes applied on top of the VisionR1 snapshot
- **selinux submodule URL** (`.gitmodules`): the upstream `1q23lyc45/selinux` remote is unreachable
  (HTTP 404). Re-pointed to `LSPosed/selinux`, which hosts the **identical pinned commit
  `8c6acc0d7792cda5f203dfd8e94c633e9dbfdeae`**. Without this, `git submodule update` (and therefore
  CI checkout) fails.
- **`init-ld` linker fix** (`native/src/Android.mk`): disable LTO for the tiny `libinit-ld.so`
  preloader (`-fno-lto`). The ondk r27.1 `lld` segfaults during LTO codegen for an aarch64 shared
  object on some hosts (notably Windows); LTO is pointless for a single-file preloader, so this is
  behavior-neutral.

## Zygisk
Built-in Zygisk was removed upstream (commit `2ef8f00`, *"Remove Zygisk"*) due to multiple security
vulnerabilities and is no longer supported. Use **[ReZygisk](https://github.com/PerformanC/ReZygisk)**
for Zygisk support (it provides the Zygisk API for Magisk/Kitsune, KernelSU and APatch).

## Building
```sh
export ANDROID_SDK_ROOT=/path/to/android-sdk
python build.py ndk        # install the ondk (Magisk NDK, r27.1)
python build.py -r all     # release  -> out/app-release.apk
python build.py all        # debug    -> out/app-debug.apk
```
APKs are universal (armeabi-v7a, arm64-v8a, x86, x86_64).

## CI
`.github/workflows/android.yml` builds **release + debug** for **all four ABIs** on every push to
`kitsune`, and publishes a **versioned GitHub Release** (`v31.0-<build>`, one permanent release per
build — canary-style, the way Magisk forks shipped during active dev) with both universal APKs.
