# PR6 next-system baseline

Status: implementation and local qualification complete on 2026-08-01.

## Scope

The official release API was rechecked at branch cut. Magisk v30.7 remained the latest stable
release, so `next-system` was created directly from
`e8a58776f1d7bdf852072ad0baa6eceb9a1e4aac`. Before any fork metadata was applied, the detached,
unmodified source and exact recursive submodules built successfully in both debug and release for
`armeabi-v7a`, `arm64-v8a`, `x86`, and `x86_64`.

The pre-baseline Kitsune head `c02320a92e9b4b1e1dc25624f9e3d6db2ad3603e` remains on branch
`kitsune` and is protected locally by annotated tag `kitsune-pr5b-reference-20260801`.

PR6 intentionally adds no Direct-System/System Mode, MagiskHide/SuList, early-mount, or
external-Zygisk implementation. It preserves upstream normal installation routes, built-in Zygisk,
and the release-daemon package-signature checks. The experimental application ID is separate from
both official Magisk and the prior Kitsune manager.

## Pristine v30.7 evidence

| Item | Result |
|---|---|
| Release all-ABI build and test APK | Passed |
| Debug all-ABI build and test APK | Passed |
| Release APK SHA-256 | `4a3f1aec243b2693c4e73f9f7abd088253ce184b719272de06b16ac337b16d69` |
| Debug APK SHA-256 | `1aac0d4138fd93b901c60c04b93db10d826e8ad5f2ce2b0f3642d8119d48b986` |
| Test APK SHA-256 | `fd16c8ad66e6d232155c3131a5df46095ad86bf571fb981ca0ee406c32daebd3` |
| Upstream fallback certificate SHA-256 | `29b04c032802179a86374c8a13dba9ddbe8d9303f4909ac03766cb77de17c027` (Android debug; forbidden for the Kitsune PR6 release artifact) |
| Python 3.11 pristine invocation | Failed before execution because v30.7 used Python 3.12 nested-f-string grammar while claiming Python 3.8+ |
| Linked-worktree Gradle invocation | Failed because the build passed the `.git` pointer file to JGit as a repository directory |

The two build portability defects are repaired in the baseline delta: the script now truthfully
requires Python 3.9+ and uses syntax accepted by that minimum, while Gradle discovers both normal
and linked-worktree Git directories through JGit's public repository builder.

## Toolchain and source contract

The machine-readable record in `next-system-baseline.json` is authoritative for the exact gitlinks,
toolchain, source-delta allowlist, and advisory dispositions. Local pristine builds used JDK 21,
Python 3.13, Gradle 9.3.0, Android Gradle Plugin 9.0.1, compile SDK 36.1/build-tools 36.1.0, and an
isolated ONDK r29.5. Python 3.9 is the declared minimum and is checked separately with Python 3.11.

`tools/next_system_baseline.py` fails closed on an unexpected source file, changed gitlink, missing
ordinary install surface, missing signature enforcement, incorrect package/version/channel source,
wrong APK metadata, absent ABI payload, duplicate ZIP member, bad ELF class, missing 16 KiB
alignment, manager/daemon identity mismatch, invalid APK signature, or release/debug signer reuse.

## Dependency audit boundary

The pristine Cargo graph was audited with cargo-audit 0.22.1 against advisory database commit
`8c1eed913b9c51b5bf817bd4fe724cb049f0406c` (1,178 advisories, 155 resolved dependencies). The
official stable graph has one RSA vulnerability without a published fix, two current unsoundness
findings (`anyhow` and `cxx`) with fixes, and two yanked transitive versions. PR6 records rather than
silently changing this privileged stack. The fixed `anyhow`/`cxx` graph and yanked dependencies are
explicit blockers before PR7 can be promoted; their update must rerun all ABI, patch, boot, signer,
and device gates.

## Minimally branded qualification

The reviewed source was copied into a detached linked worktree so the existing workspace build
outputs and ignored developer files were not reused. The exact candidate then passed independent
debug and release all-ABI builds. Its offline source/artifact verifier passed package, version,
install payload, ELF identity/alignment, ZIP alignment, native identity/mode, APK signature, and
signer-separation gates.

| Candidate artifact | SHA-256 | Signing certificate SHA-256 |
|---|---|---|
| Debug manager | `255dbba880c320572549463f5a6c4428c31e1f57793005233d0f200278bd004f` | `29b04c032802179a86374c8a13dba9ddbe8d9303f4909ac03766cb77de17c027` |
| Release manager | `fb90b36b0e4e08a7393184c1d7df9f8fe69cf6107fedae869bbc2dc70a556d65` | `2fa97316a256d8915e094faeee3d55be8695d3a420f43c3e971a9c9ae768da25` |
| Debug instrumentation | `326ec1d83df479eebdfeb6825e543b88d3793581b186604136c3f2c42e51e00f` | `29b04c032802179a86374c8a13dba9ddbe8d9303f4909ac03766cb77de17c027` |
| Release instrumentation | `fd990ab0f346fc88ef8d022f1747decea6b96c1b617bab1a4299f82eb348fbb1` | `2fa97316a256d8915e094faeee3d55be8695d3a420f43c3e971a9c9ae768da25` |

The release certificate was generated only for this disposable local qualification run. Debug and
release instrumentation APKs are retained as separate artifacts because Android requires each to
match the manager it targets. The instrumentation application ID follows the experimental manager
as `io.github.huskydg.magisk.next.test`, preserving the upstream hide/restore repackaging contract.

### Local device matrix

| Android/API | Image | Debug | Release | Concurrent root stress |
|---|---|---|---|---|
| Android 6.0 / 23 | `default`, ARM64 | Passed | Passed | 48/48 passed per signer lane |
| Android 11 / 30 | `aosp_atd`, ARM64 | Passed | Passed | 48/48 passed per signer lane |
| Android 15 / 35 | `aosp_atd`, ARM64 | Passed | Passed | 48/48 passed per signer lane |

Each of the six clean-userdata lanes covered host-side ramdisk patching, patched boot, expected
debug/release daemon identity, manager and test installation, environment setup, reboot
persistence, ten core instrumentation tests, built-in Zygisk/module behavior, manager hide,
repackaged-manager operation, restore, and post-restore operation. The root stress issued eight
rounds of six simultaneous `su -c id` requests per lane and rechecked daemon version code 30700
after every round: 288/288 candidate root requests passed.

The existing MuMu instance was reached through both `emulator-5554` and `127.0.0.1:16384`; matching
boot IDs and fingerprints proved they were the same Android 12/API 32 ARM64 instance. Read-only
checks found its pre-existing `31.0-kitsune:MAGISK:R` daemon (version code 31000), and 48/48
concurrent root requests passed. No candidate APK was installed there, so this is bridge/root
availability evidence rather than candidate behavior evidence. The instance was not patched,
rebooted, wiped, uninstalled, or deleted, and the temporary TCP alias was disconnected.

Local `shared:lintDebug` and `stub:lintDebug` passed. `apk:lintDebug` still reports two findings
already present in the pristine v30.7 source: the intentional deny action in
`SuRequestActivity.onBackPressed` lacks a super call, and the internal VIEW activity is treated as
an implicit external launch. The APK unit-test task has no sources. These are recorded upstream lint
debt, not silently suppressed in the minimal baseline delta.

### Cleanup proof

Cleanup is verified immediately before handoff: all temporary AVD definitions and data, the newly
downloaded API 23 and API 30 system images, the isolated SDK/ONDK, detached candidate worktree,
build artifacts, disposable signing key, logs, audit clones, and temporary Homebrew build utilities
were removed. Pre-existing Android SDK images, workspace outputs, and the running MuMu
`emulator-5554` instance were preserved.

No AVD or MuMu result in this section is treated as Direct-System evidence; that implementation
belongs to PR7.
