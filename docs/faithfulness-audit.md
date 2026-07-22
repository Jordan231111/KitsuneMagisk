# KitsuneMagisk purpose and faithfulness audit

Audit date: 2026-07-22 UTC

Audited reference: `kitsune` at `cf149fcf734539f6077cd6b349d9ffc2496c56ca`, plus the
uncommitted PR3/PR4/PR4A implementation described in the development roadmap.

Official comparison points: Magisk `v30.7` at
`e8a58776f1d7bdf852072ad0baa6eceb9a1e4aac` and the observed official `master` tip at
`14ea5cfb4a5771c742f7c3fd1e685bdbfac7aa8c`.

## Verdict

Kitsune is still recognizably faithful to its intended product, but the current branch must be
treated as a reference implementation rather than a modern stable release.

The faithful product is not “System Mode instead of Magisk.” It is the complete Magisk-derived root
and systemless-customization platform, with persistent Direct-System/System Mode as Kitsune's
release-defining addition and MagiskHide/SuList/emulator compatibility as important secondary
differences. Ordinary boot-image installation, superuser policy, modules, MagiskBoot, recovery, and
safe removal remain first-class responsibilities.

PR3 and PR4 are in scope and do not redirect either installation path. PR3 characterizes System
Mode without mutating the installer. PR4 contains broken update behavior and preserves existing
Hide/DenyList/SuList data transactionally. PR4A hardens the local lab/build flow and proves the
ordinary emulator install route. These changes should remain; they provide safety and evidence
needed by both the current and forward-port branches.

The current branch is nevertheless far behind official Magisk in Android boot/init/SELinux/SU and
Zygisk work. Updating scattered dependencies or merging hundreds of commits into it would not
honestly solve that problem. The maintainable route remains a behavior-driven System Mode
forward-port onto the latest audited official stable base, currently v30.7, followed by explicit
Hide/SuList, Zygisk, module API, and device-qualification PRs.

## What was reviewed

This was a graph and semantic audit, not a claim that 6,855 commits were each manually reread line by
line.

- The complete local reachable graph contains 6,855 commits; 6,838 are ancestors of the audited
  branch.
- The latest shared official ancestor is `154121f3` from 2024-02-02.
- Every one of the 171 fork-only commits after that ancestor was catalogued by subject and changed
  paths; the range includes 12 merge commits.
- High-impact fork commits were then diff-reviewed in the installer, init, core/daemon, hiding,
  Zygisk, SELinux, module, manager, build, update, and recovery surfaces.
- A temporary full all-branch/all-tag recursive clone of official Magisk was compared at stable and
  `master`, including all current submodule pins. The PR2 baseline is 858 stable-only commits or 971
  observed-master-only commits away from the fork, with 763 or 837 differing paths respectively.
- The current install UI and call graph were traced through Kotlin, shell, native setup, recovery,
  and uninstall code. Debug and release artifacts were built for ARM64, ARMv7, x86_64, and x86.
- A fresh disposable API 35 ARM64 AVD completed the ordinary Magisk patch/setup/reboot/root flow in
  both debug and release. Port 16384 was used only for a read-only MuMu characterization because a
  clean snapshot/restore tuple was not available.

This level of review finds fork-intent contradictions and risky subsystem changes while avoiding the
false assurance of treating commit-message reading as runtime qualification.

## Product purposes and the evidence for them

| Purpose | Historical/current evidence | Faithful maintenance rule |
|---|---|---|
| Persistent Direct-System/System Mode | `05289fb5` introduced the manager, recovery, native, init, policy, persistence, and uninstall slice; later changes added partition, `/sbin`, OTA, and emulator-specific behavior. | It is the primary Kitsune differentiator and release gate. Preserve behavior, make mutation transactional, and qualify exact writable targets. |
| Ordinary Magisk installation | The repository began as Magisk and retains file patch, direct boot-image install, inactive-slot, recovery, and emulator live-setup routes. | System Mode must complement, never replace or silently intercept, normal `boot`/`init_boot`/`vendor_boot` workflows. |
| Superuser management | Magisk daemon/SU policy, prompts, database, namespaces, logging, multiuser behavior, and manager remain present. | Correct authorization and revocation outrank hiding tricks. Debug shell auto-grant must remain debug-only. |
| Systemless customization and tools | Modules, magic mount, boot stages, BusyBox, `resetprop`, `magiskboot`, `magiskpolicy`, safe mode, systemless deletion, action scripts, and addon/update behavior are inherited or extended. | Prefer current official implementations; keep Kitsune extensions only with versioned contracts and lifecycle tests. |
| MagiskHide, DenyList, and SuList | `92c0777e` is a large Kitsune-only hiding/SuList change; later commits added module hiding, SELinux-disabled behavior, package/socket changes, and table selection changes. | Preserve measured semantics and existing data, but do not promise universal detection or attestation bypass. Test namespace and provider behavior, not UI labels alone. |
| Zygisk compatibility | The fork carried GrapheneOS fixes, then `2ef8f002` removed built-in Zygisk in favor of external providers. Official Magisk still includes and actively maintains built-in Zygisk. | Keep official built-in Zygisk during the System Mode forward-port. Decide built-in, external, or dual-provider architecture only through PR11's ADR and provider/version tests. |
| Emulator and unusual-layout support | Direct-System, writable-partition discovery, Nox `/sbin`, SELinux-disabled, GrapheneOS, and partition-expansion commits show repeated compatibility intent. | Capability and recovery evidence—not a brand name or successful compilation—defines support. Keep ARM64, ARM32, x86_64, and x86 evidence separate. |
| Hidden-manager continuity | Repackaging/stub/signature flows and later hidden-app fixes remain part of the manager. | Preserve recoverability and package identity safely; do not weaken signature trust without a documented threat model. |
| Maintainer/security-research platform | This is a newly explicit maintenance purpose rather than a historical end-user feature. It follows from maintaining privileged parsers, installers, and policy code across a large upstream delta. | Use disposable owned targets, fuzz/sanitizer/fault-injection lanes, minimized regressions, and responsible disclosure. Generic support must not depend on an undisclosed device vulnerability. |

## Installation-route audit

The two uses of “Direct Install” are easy to confuse. In Magisk terminology, **Direct Install** means
patching and flashing the active boot-family image from a rooted manager. Kitsune's separate UI label
**Direct Install (modify /system directly)** means persistent System Mode.

| User route | Current dispatch | Expected mutation | Audit result |
|---|---|---|---|
| Select and Patch a File | `method_patch` → `MagiskInstaller.Patch` | Patches a selected `boot`, `init_boot`, recovery image, or supported archive; does not choose System Mode. | Preserved. |
| Direct Install on a non-emulator | `method_direct` → `FLASH_MAGISK` → `MagiskInstaller.Direct` | `findImage()` discovers the boot-family image, `boot_patch.sh` patches it, and `direct_install` flashes it. | Preserved statically; physical-device qualification remains open. |
| Install to Inactive Slot | `method_inactive_slot` → `SecondSlot` | Finds and patches the alternate slot, flashes it, then performs OTA slot handling. | Preserved statically; physical A/B qualification remains open. |
| Ordinary install on an emulator | `method_direct` → `FLASH_MAGISK` → `MagiskInstaller.Emulator` | Uses `fix_env`/live ramdisk setup, not persistent `/system` modification. | Preserved and proven on a fresh API 35 ARM64 AVD in debug and release. |
| Direct-System/System Mode | `method_direct_system` → `FLASH_MAGISK_SYSTEM` → `Direct_system` | Calls `xdirect_install_system` and modifies the persistent system/init/policy payload. | Separate and explicit. The current UI gate is too weak and PR5/PR7 must replace it with doctor capabilities. |
| Recovery ZIP | `SYSTEMMODE=true` or `systemmagisk` name selects `direct_install_system`; otherwise `install_magisk` | Explicit selection determines persistent system versus normal boot-image install. | Semantically separate, but `find_boot_image` currently runs before the branch and can abort a bootless System Mode install. PR5/PR7 retains the fix. |

No PR3, PR4, or PR4A change modifies this dispatch. The ordinary real-device route would still be
selected on a non-emulator, and System Mode still requires its distinct action. That is a source-level
compatibility conclusion, not a claim that an untested physical device is safe to flash.

## Intentional, accidental, and unresolved divergences

| Divergence | Classification | Assessment and required disposition |
|---|---|---|
| Direct-System/System Mode | Intentional product extension | Faithful and essential. The old mutation model is not safe enough for a new stable release; keep the behavior and replace the transaction/recovery mechanics. |
| MagiskHide/SuList extensions | Intentional product extension | Faithful secondary purpose. PR4 fixes the data migration gap; PR12 must define CLI/database/namespace/provider semantics and test them across users and SELinux modes. |
| Built-in Zygisk removal (`2ef8f002`) | Intentional but architecturally unresolved | It reflects a later external-provider direction, but diverges from both original Magisk capability and current official maintenance. Do not copy the deletion into `next-system`; retain official Zygisk through parity and decide later. |
| Package signature enforcement disabled (`c12fca79`) | Security-sensitive and insufficiently justified | `ENFORCE_SIGNATURE` remains forced to zero, weakening manager/stub identity checks beyond debug builds. The pristine upstream baseline restores enforcement. PR4B must threat-model hidden-manager requirements; any exception must be narrow and tested, not global. |
| Pointer-only `hidelist` → `denylist` selection (`25fa2159`) | Accidental upgrade defect around a reasonable compatibility direction | Current PR4's v13 migration conservatively unions rows, keeps legacy/SuList state, makes a verified v12 backup, and tests interruption. Runtime semantics remain PR12 work. |
| Fake `31.0` compatibility number | Intentional workaround with misleading coupling | Split app upgrade order, Kitsune version, upstream base, module compatibility, protocol, and commit identity in PR9. Never imply newer official source. |
| Debug shell root grant (`cb5779f`) | Intentional test convenience | Acceptable only in unmistakable debug artifacts. Release and canary checks must prove it is absent. |
| App/stub optimizer/obfuscation disabled (`0128bb18`) | Intentional openness/debuggability choice with unnecessary runtime cost | GPL source availability does not require disabling safe optimization. Restore current upstream optimizer behavior with hidden-manager/release regression tests. |
| Recovery System Mode boot-image prerequisite | Accidental control-flow contradiction | Explicit System Mode should not require the normal route it exists to avoid. Fix conditionally in PR5 or directly in PR7. |
| Live SELinux permissive capability probe and non-transactional persistent writes | Legacy safety debt | Replace with read-only capability detection, an external verified backup, staging/journal/manifest ownership, rollback, and cold-boot recovery proof. |
| Broad architecture/version claims from builds | Evidence gap | Four-ABI compile/link is retained, but runtime and recovery evidence must be recorded per architecture, Android version, page size, boot layout, and adapter. |

## Zygisk and hiding direction

“Better Zygisk” and “better root hiding” are important, but combining them with the first System Mode
port would make failures impossible to attribute.

1. Start `next-system` from the latest audited official stable and keep its built-in Zygisk unchanged.
2. Port and qualify persistent System Mode without hiding enabled.
3. In PR11, measure built-in and named external provider/version choices against an explicit adapter
   contract. Current external projects do not establish one universal SuList contract.
4. In PR12, port MagiskHide/SuList core semantics with database, CLI, namespace, multiuser,
   SELinux-enforcing/disabled, and provider tests before adding UI assumptions.
5. Treat detection behavior as a measured compatibility layer. Remove tricks whose repeatable benefit
   does not justify their boot, performance, or security risk.

This order protects both purposes: System Mode becomes maintainable, while Kitsune's secondary
hiding behavior is preserved as an explicit, testable feature instead of an opaque patch stack.

## Dependencies, upstream updates, and Android support

The dependency goal is **latest compatible and evidenced**, not “largest version number everywhere.”
For privileged boot software, blindly updating Gradle, Rust, native, SDK, submodule, and action
dependencies together reduces rather than increases confidence.

- Re-check the official release list at the PR6 branch cut. v30.7 is the latest stable observed in
  this audit, not a permanent hard-coded promise if a newer stable exists then.
- Start from that pristine stable so its mutually compatible boot/init/SU/SELinux/Zygisk, Rust,
  native, app, and submodule set is inherited together.
- Maintain a machine-readable inventory of Gradle plugins/libraries, Cargo crates, vendored native
  code, submodule pins, GitHub Actions, JDK/Python/Rust/NDK/SDK requirements, advisory status, and
  deliberate holds.
- Separate security patches and small compatible updates from major/toolchain migrations. Require
  all-ABI builds, boot-image corpus patch/unpatch/sign/verify, API 23/29/modern AVD boots, and relevant
  System Mode/physical recovery tests for privileged changes.
- Do not spend months independently modernizing the old core unless another current-line release is
  actually required. Carry regression tests and risk dispositions into `next-system` instead.
- Preserve Android 6/API 23 as the manager minimum only while its ordinary install/root/module and
  recovery lane still passes. System Mode may have a narrower supported range and must say so.
- New Android support requires more than raising compile/target SDK: exercise `init_boot`,
  `vendor_boot`, GKI, SAR/2SI, 16 KiB pages, current policy formats, AVB/verity, and physical recovery.

The fresh audit currently records one unfixed medium RSA timing advisory and five informational Rust
warnings. The reachable RSA private-key operation is local boot-image signing, not a network signing
oracle; that limits the demonstrated exposure but does not erase the advisory. Vendored `cxx` and
`rand` updates belong in isolated PR4B work with the stated gates. No audit ignore should claim an
unfixed issue is resolved.

## Decision for PR3 and PR4

Keep both implementations.

- PR3 supplies the common capability/evidence contract required to compare the current line with
  `next-system`. It found a real writable MuMu layout without mutating port 16384 and correctly
  rejected modern immutable AVD layouts.
- PR4 fixes current user-data and updater hazards without changing root installation architecture.
  Its migration must also be carried into the first future build that accepts existing databases.
- PR4A is necessary lab infrastructure and caught a real ARMv7 debug-link inconsistency. Its final
  AVD result proves normal emulator setup remains independent from System Mode.

No additional boot/init/System Mode mutation should be slipped into these PRs. The newly identified
signature-enforcement, Zygisk, optimizer, real-device, and dependency work is safer and more
reviewable in PR4B and the already ordered PR6–PR16 sequence.

## Release-level conclusion

The project has preserved the right product ideas, but it has not yet earned a stable support claim.
The next maintainable release must prove all of the following together:

- ordinary boot-image and emulator-live installation still work;
- Direct-System works transactionally on exact advertised writable targets;
- both paths uninstall or restore a bootable stock state;
- SU, modules, Hide/SuList, and the chosen Zygisk model pass independent behavior tests;
- ARM64, ARM32, x86_64, and x86 claims are backed by the appropriate build/runtime evidence;
- modern Android boot/policy paths come from an audited current official base; and
- every dependency/security hold has a written disposition rather than a silently stale pin.

That is the definition of being faithful while still moving the project forward.
