# PR15 module compatibility

Pinned unmodified upstream ZIPs, clean userdata per case, prepared and tested cold boots, removal, selected functional probes, and official stable controls for failures or inconclusive probes. Providers also receive the project API v5 companion fixture.

Representative widely used modules, not an exhaustive ecosystem ranking. AVD profiles do not simulate OEM hardware. Physical audio, charging and hardware attestation are unqualified.

Candidate `31016` uses `io.github.jordan231111.magisk`, clean source `57c9c9081`.
[The machine record](module-pr15.json) pins each upstream ZIP, exact APK and test result.
The matrix covers sixteen standalone modules, seven on a 16 KiB guest, four provider/client
combinations at 4 KiB, two additional 16 KiB combinations, and API29 LSPosed/Riru checks.
The page-size limits below include failures; they are not blanket compatibility claims.
No module ZIP is redistributed.

| Module | Version | Observed result | Boundary |
|---|---|---|---|
| [LSPosed/ZygiskNext](https://github.com/LSPosed/ZygiskNext/releases/tag/v1.5.2) | v1.5.2 | Provider activation and client-module combinations passed | Only one provider enabled. Ordinary manager consent passes; stock providers are not the versioned Kitsune SuList adapter and do not imply policy parity. |
| [PerformanC/ReZygisk](https://github.com/PerformanC/ReZygisk/releases/tag/v1.0.0) | v1.0.0 | Provider activation and client-module combinations passed | Only one provider enabled. Ordinary manager consent passes; stock providers are not the versioned Kitsune SuList adapter and do not imply policy parity. |
| [LSPosed/LSPosed](https://github.com/LSPosed/LSPosed/releases/tag/v1.9.2) | v1.9.2 | Manager active on API29; inactive on API35 | The release declares Android 8.1–14 support. Both outcomes match official 30.7 controls. |
| [LSPosed/LSPosed.github.io](https://github.com/LSPosed/LSPosed.github.io/releases/tag/shamiko-414) | shamiko-414 | Incompatible with this fork | 1.2.5 refuses both debug and release Kitsune; official 30.7 and 31.0 controls work. Hiding the manager does not fix it. Zygisk Next does not list it as loaded, and selected-app mount hiding fails. |
| [snake-4/Zygisk-Assistant](https://github.com/snake-4/Zygisk-Assistant/releases/tag/v2.1.4) | v2.1.4 | Selected-app mount hiding passed | Core enforcement was disabled. A real unprivileged app saw the marker before selection and could not see it after the module took ownership. |
| [osm0sis/PlayIntegrityFork](https://github.com/osm0sis/PlayIntegrityFork/releases/tag/v18) | v18 | Install, cold boot and removal passed | Installation and lifecycle only on the account-free AOSP image. No Google integrity verdict or hardware-backed attestation claim. |
| [KOWX712/PlayIntegrityFix](https://github.com/KOWX712/PlayIntegrityFix/releases/tag/v4.9-inject-s) | v4.9-inject-s | Install, cold boot and removal passed | Installation and lifecycle only on the account-free AOSP image. No Google integrity verdict or hardware-backed attestation claim. |
| [5ec1cff/TrickyStore](https://github.com/5ec1cff/TrickyStore/releases/tag/1.4.1) | 1.4.1 | Install, cold boot and removal passed | Keystore modification and hardware attestation verdicts remain unqualified. The process-name probe was inconclusive on both current and stable cores; no external keybox was obtained. |
| [RikkaApps/Riru](https://github.com/RikkaApps/Riru/releases/tag/v26.1.7) | v26.1.7 | Installed, but injection unqualified | Riru reports not loaded on both cores in prepared API35 and API29 comparisons. Lifecycle passes; do not claim working injection. |
| [RikkaApps/Sui](https://github.com/RikkaApps/Sui/releases/tag/v13.5.1) | v13.5.1 | Server active at 4 KiB; no server observed at 16 KiB | Official 30.7 reproduces the 16 KiB linker failure. Zygisk Next/ReZygisk combinations also did not start the Sui server. Use this pinned version only within its qualified 4 KiB scope; not every downstream client is qualified. |
| [Magisk-Modules-Repo/busybox-ndk](https://github.com/Magisk-Modules-Repo/busybox-ndk/releases/tag/13614) | 13614 | Executable works at 4 KiB; segfaults at 16 KiB | The identical installed binary fails on official 30.7 too. This pinned ZIP is not qualified for 16 KiB. Kitsune’s own bundled BusyBox remains separately tested; no exhaustive applet-equivalence claim. |
| [programminghoch10/ViPER4AndroidRepackaged](https://github.com/programminghoch10/ViPER4AndroidRepackaged/releases/tag/v87) | v87 | Install, cold boot and removal passed | Candidate 31016 keeps the module enabled, publishes audio-effect configuration and permits removal on AVD and MuMu. Physical audio/DSP output remains unqualified. |
| [Magisk-Modules-Repo/magiskhidepropsconf](https://github.com/Magisk-Modules-Repo/MagiskHidePropsConf/releases/tag/v6.1.2) | v6.1.2 | Install, cold boot and removal passed | Function-specific hardware behavior is not implied by lifecycle checks. |
| [VR-25/acc](https://github.com/VR-25/acc/releases/tag/v2023.10.16) | v2023.10.16 | Standard module install and removal passed | Initial harness omitted the normal module directory; corrected tests pass both cores. Charging control requires matching physical power-supply hardware. |
| [JingMatrix/Vector](https://github.com/JingMatrix/Vector/releases/tag/v2.2) | v2.2 | Framework and manager active | Corrected action category and Compose UI checks pass on both cores. Real root consent also passes with built-in, ReZygisk and NeoZygisk combinations. |
| [JingMatrix/NeoZygisk](https://github.com/JingMatrix/NeoZygisk/releases/tag/v2.4) | v2.4 | API v5 injection passes at 4 KiB; fails at 16 KiB | The same 16 KiB failure occurs on official 30.7. Shipped ARM64 binaries have incompatible 16 KiB load layouts; use a qualified provider at that page size. Stock providers do not implement the Kitsune SuList adapter. |

Kitsune Hide and Zygisk Assistant pass actual selected-app mount visibility on 4 KiB and
16 KiB AVDs, MuMu Android Device-1 and the fresh BlueStacks instance. The marker remains
visible to root; it is hidden from the selected unprivileged app. Core enforcement is
disabled when Zygisk Assistant owns hiding. Temporary modules and list entries are removed.

Shamiko 1.2.5 remains incompatible with this signing identity. The user accepted the
Kitsune Hide / Zygisk Assistant fallback. [Causal controls](device-pr15/shamiko-signing-identity.json)
show that re-signing official Magisk reproduces the rejection with unchanged native code;
matching manager/stub signers retain manager root but do not satisfy Shamiko. The project
keeps its own signer and authenticated manager behavior.

The four 4 KiB provider combinations enable one injector at a time and verify Vector/Sui activity,
actual manager consent and clean removal. Stock providers do not implement the
[versioned Kitsune SuList adapter](adr/0001-zygisk-provider.md). These results qualify
the listed functions and targets; hardware audio, charging and attestation remain unqualified.

[16 KiB limits](device-pr15/module-16k-limits.json) document Sui, NeoZygisk and third-party BusyBox failures with
the same ZIPs on official Magisk. The Sui combinations also did not produce a working server.
Builtin Zygisk, Zygisk Next and ReZygisk pass the API v5 fixture at 16 KiB; this does not
make every client module compatible. A successful install does not certify injection.
