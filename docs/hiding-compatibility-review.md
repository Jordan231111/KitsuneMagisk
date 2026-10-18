# PR15 hiding and provider review

This review keeps the [Kitsune Hide/SuList contract](hide-policy.md) and
[single-provider boundary](adr/0001-zygisk-provider.md) authoritative. Module
compatibility must not change root consent, manager signature verification,
database ownership, or the meaning of an existing selection.

## Reference scope

The source review on 2026-10-17 used these exact revisions:

| Project | Revision | Material reviewed |
| --- | --- | --- |
| [KernelSU Next](https://github.com/KernelSU-Next/KernelSU-Next/tree/b28fce510568ba0b7584b38492cabfaaeb61ac37) | `b28fce510568ba0b7584b38492cabfaaeb61ac37` | UID transition hook, allowlist/profile policy, kernel unmount path and SUSFS policy gate |
| [SUSFS](https://gitlab.com/simonpunk/susfs4ksu/-/tree/2d11d711de80f0f749b3055f8da8f7b9682454e4) | `2d11d711de80f0f749b3055f8da8f7b9682454e4`, Android 15 / 6.6 branch | Integration prerequisites, kernel feature switches and filesystem patches |
| [Zygisk Next public repository](https://github.com/LSPosed/ZygiskNext/tree/db994f8caebd440eedd02beab38ca62f3d1f8a2a) | `db994f8caebd440eedd02beab38ca62f3d1f8a2a` | Requirements, public native API and HyperOS runtime contract |
| [Zygisk Next release](https://github.com/LSPosed/ZygiskNext/releases/tag/v1.5.2) | `1.5.2 (864-a4a72ce)` | Unmodified release installer, boot/service/cleanup scripts, ELF layout and runtime comparisons |
| [REA](https://github.com/morluto/rea/tree/63c65a22430a4bbc0c1807a0298edb867775001f) | CLI `rea-agents@6.5.0` | Local Rizin-backed ELF/import/symbol inspection; no agent configuration changes |

Zygisk Next's current implementation source is not published in that repository.
Its public headers, scripts, release binaries and measured behavior can be
reviewed; a complete source audit cannot be claimed. No Zygisk Next binary or
implementation code is incorporated into this fork. REA is an analysis tool,
not an alternative root provider.

## Kernel mechanisms and the portable boundary

KernelSU Next calls its unmount policy during the process UID transition. Its
kernel unmount path checks both the target UID/profile and the originating
zygote SELinux context before removing registered mounts. The context check
matters: a root process changing UID can still inhabit the global namespace.
Kitsune retains its own verified-zygote and private-namespace checks; treating
every UID above 10000 as a safe unmount target would lose those protections.

SUSFS implements path, mount, stat, mapping, uname and boot-information behavior
inside a matching kernel. Its userspace module alone cannot provide those hooks.
The reviewed KernelSU Next SUSFS gate also queries KernelSU's actual unmount
policy rather than assuming every application UID is excluded. These mechanisms
depend on kernel code, symbols and integration that a normal Magisk installation
on an unchanged stock kernel does not provide. They are not advertised as
portable Kitsune features or emulated by altering root authorization.

Kitsune's portable policy remains:

- Normal Hide removes root/module mounts from selected processes.
- SuList removes those mounts and module callbacks from unselected processes;
  selection itself never grants root.
- The verified manager and recovery routes remain usable. Hidden-manager
  registration survives unrelated package-directory removal, but an unverifiable
  manager still receives no authorization.
- Built-in Zygisk enforces at specialization. The Zygisk-off log engine remains
  asynchronous and does not promise cleanup before an app's first instruction.

## Zygisk Next interoperability

The tested ZIP SHA-256 is
`ce50b13fecb9e4456db69ef17b715f7cfbc094004dd9f30f7d6348dfab619a96`.
Its installer requires Android 8 or later and Magisk compatibility code 26402 or
later, checks the selected root implementation, verifies extracted native files,
and selects native/compatibility binaries from the advertised ABIs. Its early
script starts `zygiskd daemon`; the service stage is skipped when built-in Zygisk
is enabled. Cleanup and removal also remove its Magisk compatibility module.

All 19 shipped ELF files were inspected. The ARM64 and x86_64 load segments are
congruent at 16 KiB boundaries; several ARM32, x86 and RISC-V segments are not.
This is a file-layout observation, not a runtime qualification or a claim that
those ABIs use a 16 KiB kernel. ELF imports and strings also cannot establish
the correctness of an obfuscated provider's internal root-detection logic.

Candidate `31016` additionally passes the real API v5 module-directory, root-companion,
flags and post-specialization probe with Zygisk Next and ReZygisk on both 4 KiB and 16 KiB
guests. NeoZygisk 2.4 passes at 4 KiB but fails that probe at 16 KiB on both Kitsune and
official Magisk. Sui 13.5.1 also lacks a working 16 KiB server, and the third-party
BusyBox 13614 binary segfaults at 16 KiB under both cores in the tested configurations.
The [paired controls and ELF layouts](device-pr15/module-16k-limits.json) retain those limits;
the successful provider probes do not imply compatibility for every client module.

The public Zygisk Next API describes native init-process injection, hook and
symbol-resolution callbacks, and companion sockets. The HyperOS runtime API is
native-only and explicitly lacks ART/JNI objects. Its API version is independent
of the standard Zygisk module API number. Those declarations do not establish a
Kitsune SuList adapter or qualify unavailable HyperOS devices.

The [module matrix](module-compatibility.md) records provider activation, actual
client modules, ordinary root consent, removal and controls. A running provider
or a successful ZIP install does not prove that it applied Kitsune's selection
policy. Stock Zygisk Next remains outside the versioned SuList adapter contract;
the manager/core must reject conflicting enforcement rather than silently enable
two owners of an app's mount namespace. Built-in Zygisk remains available.

## Shamiko acceptance

Shamiko 1.2.5 (414), SHA-256
`308d31b2f52a80e49eb58f46bc4c764a6588a79e4b8d101b44860832023f88b4`,
has a signing-identity compatibility requirement, established by the
[causal controls and syscall observations](device-pr15/shamiko-signing-identity.json).
The same unmodified ZIP reports unsupported on the fork and works on official
Magisk 30.7 and 31.0 in the same API 35 AVD family. It reads the daemon's preserved
`stub.apk` through `/proc`, parses its signing certificate, then returns `-1` on
this fork; the official signer proceeds and returns `0`.

Re-signing only official 30.7's embedded stub reproduces the rejection with
unchanged native code, version metadata and module bytes. Re-signing both the
full manager and stub with the same disposable key also reproduces it, even
though verified-manager root works. The stub's non-signature entries and ZIP
comment are unchanged; all 681 non-signature/non-stub entries of the full manager
are unchanged. This rules out Kitsune's package name, version separation and
native Hide/SuList changes as necessary causes of this rejection.

The module must accept this project's signing identity for normal compatibility.
Replacing the genuine embedded stub affects the daemon's manager trust anchor
and hidden-manager recovery. A borrowed official stub or falsified identity is
not a production fix. The module's closed implementation does not expose its
complete internal trust policy; no claim is made that this is its only check.
Diagnostic re-signed APKs and keys are not shipping artifacts. The user accepted the documented Kitsune Hide / Zygisk Assistant fallback for PR15
on 2026-10-17; Shamiko remains explicitly unsupported.

Any proposed fix must pass the actual selected-app visibility probe as well as
startup, consent, Hide, SuList, provider exclusivity, hidden-manager recovery,
module removal and cold-boot checks. Kitsune Hide and Zygisk Assistant have
already passed that visibility probe on their recorded artifacts; this does not
turn the failing Shamiko result into a pass. Diagnostic instrumentation is kept
separate from acceptance using the ordinary unmodified module ZIP.
The final `31016` AVD comparisons repeat both fallback paths on 4 KiB and 16 KiB kernels:
the selected unprivileged app loses the marker while root retains visibility.
Zygisk Assistant 2.1.4 explicitly skips apps with `PROCESS_GRANTED_ROOT` in its
[specialization callback](https://github.com/snake-4/Zygisk-Assistant/blob/v2.1.4/module/jni/main.cpp).
The commercial-emulator probe therefore removes and later restores its own test UID's
grant left by the SU suite, and temporarily disables the other hiding fixture. The core
continues reporting truthful authorization flags; this is test isolation, not a policy bypass.


## Independent detector comparison

[Native Root Detector 7.7.0](https://github.com/reveny/Android-Native-Root-Detector/releases/tag/v7.7.0)
was also run unmodified against stock, candidate and official 30.7 Android 10 ARM64 AVDs.
The [exact APK, boot IDs and observations](device-pr15/hiding-detector-comparison.json) retain
its default settings: experimental and custom-ROM checks off. Its native implementation is not
fully public; the report is an independent observation, not a complete security audit.

| Configuration | Findings beyond the stock SDK debug fingerprint |
| --- | --- |
| Stock SDK | None |
| Kitsune, no hiding and no Zygisk | Magisk and SU binary |
| Kitsune Hide or SuList, built-in Zygisk | Mount gap, built-in Zygisk, suspicious mount |
| Kitsune plus Assistant, built-in Zygisk | Mount gap and suspicious mount |
| Official 30.7, built-in Zygisk with DenyList | Mount gap, built-in Zygisk, suspicious mount and recognized root-app flags |
| Kitsune Hide or SuList, Zygisk disabled | None |
| Kitsune with Zygisk Next plus Assistant | Mount gap |

The last row records that installed configuration and the detector's result. Its API29 provider
activation was not separately probed; do not use that row alone to certify working injection.
The independent Next API v5 probes remain the separately qualified API35 4/16 KiB runs.

For this detector on this image, no-Zygisk Hide/SuList produced the fewest extra findings.
It also disables modules that require Zygisk. Built-in Hide/SuList still fulfills its tested
namespace/policy contract, but cannot be described as concealing every injection or mount trace.
The same trace categories appear on official Magisk; this comparison does not identify a
Kitsune-specific core regression. No-Zygisk cleanup remains asynchronous, so these observations
cannot establish a first-instruction guarantee for other applications. Kernel-patched SUSFS
behavior and hardware attestation remain separate capabilities.
