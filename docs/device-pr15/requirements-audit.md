# PR15 requirement audit

The completed baseline is `31016`, source `57c9c90813b65015d530ac119141baf32dddb9bd`.
Candidate `31017` adds the [Waydroid fixes](waydroid-diagnosis.json) and still requires a consistent
signed build and runtime qualification.
The requested AVD, MuMu Device-1 and fresh BlueStacks runs have acceptance evidence.
That is narrower than every target and lifecycle in the original PR15 checklist.
This audit keeps those distinctions explicit; moving a row to a release gate does not
make its test pass. The compatibility table continues to label accepted targets experimental.

| Roadmap requirement | Evidence and disposition | Remaining boundary |
| --- | --- | --- |
| Exact MuMu, AVD and BlueStacks lanes | [Generated records](../../compatibility/TESTED.md), [MuMu lifecycle](mumu-lifecycle.json), [BlueStacks lifecycle](bluestacks-lifecycle.json). The final candidate passes eight local debug/release lanes and both named commercial targets. | LDPlayer and Nox were explicitly excluded by the user. No vendor-wide claim. |
| Real-device and immutable-layout lanes | SDK boot-image routes pass. Read-only layout classification/refusal remains covered by the host contracts; BlueStacks Direct-System is refused. | Physical OEM, encrypted storage and real immutable-device installation remain unqualified. |
| Ordinary patch/direct/inactive-slot/recovery routes | [Boot corpus](pr15-35-4k-boot-fixtures-release.json), [Cuttlefish Direct Install](cuttlefish-release.json), manager file/tar processing and stock recovery records. | Synthetic boot headers do not qualify actual OEM bootloaders. Physical OTA/inactive-slot and recovery ZIP cycles remain unqualified. |
| Authoritative SU policy matrix | Final 120-test app suites, concurrent root requests, [manager contract](../manager-pr13.json) and [CI acceptance](ci-acceptance.json). | Exact tested emulator images and runtime ABIs only; physical/user-profile variants are not inferred. |
| Hidden-manager lifecycle | Final normal/hidden manager suites, authenticated migration/recovery and the package-removal race regression. | Historical signer/package upgrade records retain their original source identities. No arbitrary predecessor APK or OEM qualification. |
| Recovery/ZIP System Mode and custom-ROM replay | [Explicit route decisions](../device-qualification.md): managed System Mode requires live target/host authorization; recovery `SYSTEMMODE` and automatic managed `addon.d` replay are unsupported. Ordinary inherited routes remain available. | A real ordinary recovery/OTA cycle is still required before advertising it. |
| Vivo, emulator PATH, disabled-SELinux/container paths | Exact Vivo signature fixture and emulator PATH/bootstrap checks pass. [Stock SDK attempts](selinux-disabled-attempts.json) demonstrate why the tested kernel does not provide disabled SELinux. | Permissive is not disabled. Waydroid and a suitable SELinux-disabled kernel remain unqualified. |
| Pre-init storage and expanded partitions | [Module contract](../module-contract.md), [PR14 evidence](../module-pr14.json), final module suites and the pre-init parser regression. Negative persist/space/stale-copy and transactional fault cases remain covered. | Only the retained partition API is supported. OEM-specific partitions and encrypted physical recovery require matching targets. |
| Emulator lifecycle, upgrade, host restart, clone, restore and failure injection | Cold starts, manager/core upgrade records, snapshot/image restoration, module failure cases, vendor-root-off checks, uninstall and original-state restoration are recorded. | A Mac host reboot, vendor emulator-version upgrade and commercial-instance clone survival were **not** qualified. Exact current versions are the unit of support; these lifecycle extensions must not be advertised as passed. |
| Machine-readable records and compatibility page | `tools/device_qualification.py render --check` validates the linked records and generated outputs. | Experimental support only; production promotion remains PR16. |
| One-instance ownership and cleanup | MuMu Device-1 restored byte-for-byte and stopped; fresh BlueStacks instance unrooted/deleted, factory initramfs restored. Original-primary restart hits a port conflict with the unrelated running MuMu Device-0; the user chose to leave BlueStacks stopped. | Final temporary-file cleanup is recorded separately when completed. |

## Hiding and historical behavior

[Kitsune Hide/SuList](../hide-policy.md) defines the retained behavior, process/UID rules,
provider ownership and recovery. Tests distinguish namespace visibility from root permission.
The final module comparisons prove selected-app mount hiding with both the built-in engine and
Zygisk Assistant on 4/16 KiB AVDs, MuMu and BlueStacks. They do not certify universal detector,
banking-app or hardware-attestation invisibility. The [independent detector comparison](hiding-detector-comparison.json) records additional mount/injection findings with built-in Zygisk and stock-equivalent findings with no-Zygisk Hide/SuList on one Android 10 SDK image.

The [historical ledger](../port-ledger.md) records preserved behavior, tested replacements and
explicit retirements. It is not a claim that every historical mechanism was copied: unrestricted
init scripts, unconditional runtime relocations, the old fixed-PID ptrace monitor and separate
Lite distribution have documented replacements or retirements. Their user impact and migration
remain part of the contract. The production audit still rejects unavailable target/archive gates.

## Community reports considered

These are user reports and maintenance requests, not independent confirmation of this candidate.
They motivate checks or boundaries rather than automatic changes to root authorization.

| Report | Treatment in this fork |
| --- | --- |
| [Assistant reports of apps detecting root](https://github.com/snake-4/Zygisk-Assistant/issues/81) | Avoid a blanket concealment claim. Keep actual per-app visibility checks and separate detector observations from attestation. |
| [Inconsistent mounts with different modules](https://github.com/snake-4/Zygisk-Assistant/issues/23), [mount/injection reports](https://github.com/snake-4/Zygisk-Assistant/issues/84) | Preserve one namespace owner, test combined providers/clients, and retain failures instead of treating module registration as successful hiding. |
| [DenyList apps retaining root](https://github.com/snake-4/Zygisk-Assistant/issues/89) | Document that Hide selection and SU authorization differ; SuList explicitly gates SU. Do not falsify granted-root flags to satisfy a hiding module. |
| [KernelSU Next kprobe hiding report](https://github.com/KernelSU-Next/KernelSU-Next/issues/302), [legacy SUSFS manager request](https://github.com/KernelSU-Next/KernelSU-Next/issues/1215) | Kernel-dependent behavior and manager/kernel version compatibility require their own evidence. SUSFS is not presented as a portable stock-kernel Magisk feature. |

Provider-conflict diagnostics, recoverable module updates, precise unsupported-route guidance,
and stable capability/version metadata are implemented improvements with measurable contracts.
This review does not claim to exhaust all forum requests or make every proposed feature suitable
for the qualified v31.0 candidate.
