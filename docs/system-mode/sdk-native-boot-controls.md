# SDK native boot controls

These are test-image exceptions, not device support claims. The native crash gate still rejects
Magisk, root-service, app and zygote crashes, unknown images or signatures, and failures first
recorded after the functional tests started.

On 2026-09-10, unmodified SDK images were booted on disposable hosted runners with emulator
37.2.8.0. No Magisk APK, native library or patched image was installed in the controls.
[Initial controls](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/34428832519),
[three-reboot controls](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/34429304579), and
[additional controls](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/34430157868)
recorded the complete stock boot/crash logs and boot identities. The API 25 follow-up also used
[the rooted lane's kernel launch options with an unmodified ramdisk](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/34430966199).
An [API 24 x86 control](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/34445831805)
completed a stock cold boot and eight reboots without reproducing its rooted-lane failure.

| SDK image | Observed stock failure |
|---|---|
| API 24, x86_64, Android 7.0 `NYC/4174735` | The 32-bit media extractor aborts in `libminijail::log_sigsys_handler` because its startup path calls `nanosleep`, which its seccomp policy denies. Reproduced on two stock reboots. |
| API 24, x86, the same Android 7.0 build | The rooted lane recorded that same 32-bit `mediaextractor`/`libminijail` failure and explicit denied `nanosleep`. Classification with the reproduced x86_64 image failure is an inference from the same mechanism; the separate x86 stock samples above did not reproduce it. |
| API 25, x86_64, Android 7.1.1 `NYC/4931657` | The rooted lane recorded the same explicit denied `nanosleep` and `libminijail` handler signature. It was not reproduced in the stock samples; classification as the same platform defect is an inference from that identical failure mechanism, not a claimed stock reproduction on this version. |
| API 28, x86_64, Android 9 `PSR1.180720.012/4923214` | SurfaceFlinger aborts on an unchecked HIDL `DEAD_OBJECT` result from `Hwc2::impl::Composer::getActiveConfig`. Reproduced during a stock cold boot. |
| API 28, x86, the same Android 9 build | The same SurfaceFlinger/HWC failure was reproduced on a stock reboot. |

For these signatures, acceptance requires the exact SDK fingerprint, process and crash signature;
the same timestamped crash must already be present in the log captured before the app tests. The
media-extractor case additionally requires its matching PID's explicit `blocked syscall: nanosleep`
message. The corresponding Binder service must be available after the tests. A new or unrecovered
failure still fails the lane. No device policy, seccomp rule, framework binary or crash log is
modified to obtain a pass.

The separate emulator modem failure was fixed in the harness. The emulator derives its modem
profile directory from the selected ramdisk directory; placing a patched ramdisk elsewhere lost
its SDK companion data. Patched images now live in an owned temporary directory with a link to
that SDK data. The SDK inputs remain unchanged. The lookup is visible in the
[AOSP emulator source](https://android.googlesource.com/platform/external/qemu/+/emu-master-dev/android-qemu2-glue/main.cpp).
