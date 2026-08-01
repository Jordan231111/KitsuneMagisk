# Security and upstream-differential lab

PR4B turns the one-time upstream audit into reproducible inputs. The lab does not automatically merge
or silently suppress anything: a new sensitive commit, dependency, advisory, missing gitlink, or
architecture claim must have an explicit disposition and targeted gate.

## Reproduce the ledgers

Fetch official refs without recursively fetching historical submodule pins:

```sh
git remote add upstream https://github.com/topjohnwu/Magisk.git
git fetch --force --no-recurse-submodules upstream \
  refs/tags/v30.6:refs/tags/v30.6 \
  refs/tags/v30.7:refs/tags/v30.7 \
  refs/heads/master:refs/remotes/upstream/master
python3 -m tools.security_lab.upstream_ledger --resolve-latest --check
python3 -m tools.security_lab.signature_contract --check
python3 -m tools.security_lab.rustsec --check
python3 -m tools.security_lab.submodules --historical --probe-remote --enforce-policy
```

After building exact debug and externally signed release candidates, bind their byte and signer
identity to the report:

```sh
python3 -m tools.security_lab.artifact_contract \
  --apksigner "$ANDROID_SDK_ROOT/build-tools/34.0.0/apksigner" \
  --apk out/app-debug.apk --apk out/stub-debug.apk \
  --apk out/app-release.apk --apk out/stub-release.apk \
  --reject-certificate a9342f305e5d7ecc0245f86c931226267389358c48139f5d7ed6a80cd4329629 \
  --expected-release-certificate "$KITSUNE_RELEASE_CERT_SHA256" \
  --output out/artifact-contract.json
```

The upstream ledger lists every security-sensitive common-ancestor-to-stable, v30.6-to-v30.7,
post-stable master, and fork-only commit, the owning surface, and the required gates. `master` is
observation-only. A newly published official stable makes generation fail until the baseline audit
is deliberately rerun.

The historical audit currently records six reviewed, unreachable legacy pins (four from the deleted
standalone `resetprop` repository, one old manager pin, and one old `mincrypt` pin). Any additional
loss—or restoration that makes an old exception stale—fails the policy gate.

The dependency inventory combines Cargo lock data, Gradle's resolved debug runtime graph, gitlinks,
Actions, and toolchain/API/ABI fields. `security/generated/sbom.spdx.json` is an SPDX 2.3 snapshot;
`NOASSERTION` is reported as an inventory gap, never guessed into a license.

The `baseline` inventory is intentionally anchored to immutable commit `f943ecdd`, not the dirty
worktree used while generating a PR. It retains declared build plugins, kapt/codegen, and test-only
coordinates alongside selected runtime versions. The lack of a root Rust toolchain pin is recorded
as `rust_toolchain_pinned: false`; PR4B inventories that reproducibility gap rather than inventing a
compiler version from one developer machine. The scheduled workflow separately pins
`cargo-audit` 0.22.1.

The artifact contract is run against every candidate APK rather than against a mutable build
directory. It verifies ZIP uniqueness/integrity, embedded utility/daemon version agreement,
debug/release native mode and signer separation, all required ABI payloads, ABI-to-ELF class/machine
identity, program-header bounds, and 16 KiB-compatible ELF and uncompressed-library ZIP alignment.
Its JSON output binds the
candidate certificate, byte length, and SHA-256 to those facts. Production runs also pass the
protected expected release certificate. The historical repository test certificate must be passed
through `--reject-certificate` and is forbidden for release.

## Parser corpus and sanitizer lane

The normal AVD matrix runs malformed boot/policy inputs through the exact APK binaries. The scheduled
security workflow additionally builds the C/C++ and libsepol surfaces with UBSan and runs them on a
stock disposable AVD:

```sh
./build.py -v binary --sanitize undefined magiskboot magiskpolicy
KITSUNE_SECURITY_CORPUS_ITERATIONS=32 scripts/security_avd_test.sh 35
```

The AVD script refuses to replace an existing name, uses a distinct console port, never patches the
SDK image, and deletes only the AVD it created. The device runner uses one validated
`/data/local/tmp/kitsune-security-*` directory and removes it in `finally`, including on a parser
failure. Build support for four ABIs is kept separate from runtime proof.

The opt-in build uses compiler-rt's static minimal UBSan runtime. Only `shift-base` is disabled:
both the frozen and v30.7 libsepol pins construct one fixed Android flag with signed `1 << 31`.
Invalid shift exponents and every other UBSan class remain fatal; the reviewed hold is documented as
`KITSUNE-2026-003` and must be reconsidered with the PR6 selinux pin.

For an already running authorized lab target, pass one explicit serial:

```sh
python3 -m tools.security_lab.device_corpus \
  --serial 127.0.0.1:16384 \
  --apk out/app-debug.apk \
  --iterations 32 \
  --output /tmp/mumu-parser-corpus.json
```

This parser command does not install root or mutate `/system`. It pushes temporary binaries and
inputs, tests boot sign/verify and readable-policy load/save/reload, compares canonical printed AV
rules rather than nondeterministic binary bytes, then removes its sandbox.

## PR4B qualification record

The final local ARM64 sanitizer corpus ran 977 cases independently on MuMuPlayer
`127.0.0.1:16384` and a disposable API 35 Google APIs AVD. Both runs reported zero crashes and
passed boot sign/verify plus policy save/reload; only unique `/data/local/tmp/kitsune-security-*`
sandboxes were used, and the disposable AVD was deleted. MuMu's system and instance data were not
modified because no verified snapshot/restore tuple exists.

After switching back from UBSan, a clean four-ABI release build and a four-ABI debug build passed.
The shipping debug and release APKs then independently completed patched-ramdisk boot, manager
install/setup, reboot, provider tests, a root-context check, and 137 parser cases on a new API 35
ARM64 AVD. The runner byte-restored the SDK ramdisk and deleted the AVD. These are normal Magisk and
parser-lab results, not System Mode installation or physical-device recovery evidence.

The 2026-07-31 current-line comparison used one installed BlueStacks Air 5.21.782.7501
`Tiramisu64` instance at `127.0.0.1:5555`; the final record did not use an application or data
clone. A temporary copy of BlueStacksRootMac reinjected, cold-booted, and attested these exact
manager/backend pairs in sequence:

- released comparison APK `fac319d2de262fcfff1684e13e1a5c61c486d2a773a7a8ffcfdbfe6f763a7fd4`
  (`31.0-kitsune:MAGISK:R`);
- intensive-test debug APK `425fbfe32123f92d3a4d6134f53c697b89930c3fc399a13922914d27d973bf05`
  (`bcdf65f0:MAGISK:D`);
- intensive-test release APK `38f6dd7d920b6c863179580054d9e31db9a00a21829b30af8dbd8be980bb5175`
  (`bcdf65f0:MAGISK:R`).

The released backend reproducibly terminated `magiskd` on `SELECT NULL` with `read (0 != 4)`.
The fixed debug backend returned an empty field, retained the same daemon PID, and passed 320
concurrent `su` plus 160 concurrent NULL queries. The fixed release passed a separate 160/80 run.
Both ended with root available and `PRAGMA quick_check=ok`; all temporary stress files were removed.

After the narrow file-picker cancellation backport and the final 16 KiB linker/ZIP contract were
added, clean release-then-debug builds produced this exact final set:

- debug manager `19dd28b9b9966047c55e6732204316edc82a8dde0b9585fe88c2cb2f110f723b`;
- debug stub `a41bba2159b80fbe9f9db8b2b6d0d710c089b1f2352d2d3239629414f82ebaf0`;
- release manager `d5f978f08fd2c82fbffdb598dbed376e47028d575db2a49c37e81b5550b9e484`;
- release stub `693b73167f943a66fab361dbd99a995cede6847d55a46f5f10cb03d8100f0c84`;
- schema-3 artifact report `575fb8943decfacdecb0a17760c8af811591c7549a0fd922b96b296ec534f1b3`.

The contract verified all four APKs, debug certificate
`29b04c032802179a86374c8a13dba9ddbe8d9303f4909ac03766cb77de17c027`, external lab release
certificate `e06f7fbeceb160a9a73a3df3c18e724db2c318c69fc3b2c3d15fac4e2db37b9d`, native
debug/release modes, required ABI payloads, and 16 KiB ELF/ZIP rules. The exact final debug APK was
then reinjected into the same instance. It passed initialization plus a cold boot, 64/64 concurrent
`su` calls, 32/32 concurrent NULL queries, DB integrity, module/provider checks, and exact installed
APK hashing. Canceling DocumentsUI reset Select-and-Patch to unchecked, and selecting it again
reopened the picker. The exact final release APK was contract-tested but was not reinjected after
these UI/linker-only final changes; its immediately preceding release candidate completed the live
release-mode run described above.

The live database upgrade kept the active schema at 12, created/verified the root-owned mode-`0600`
v12 backup, and wrote the complete one-time marker. The backup remained schema 12 without the
marker. Restoring the original debug payload returned the active DB to its historical version-13
label with the same logical rows.

The debug manager presented the System Mode warning and used the candidate binary's `-c` compile
identity; using `-v` here had been incorrect because it queried the already-running old daemon
during an upgrade. The guest's ext4 `/system` remained read-only, so the installer stopped at the
remount gate. Aggregate hashes of the relevant init/SELinux files were identical before and after,
and no persistent payload or `.kitsune-old` sidecar appeared. The release UI exposed only normal
patch/direct methods.

All three pre-existing module directories remained enabled, and the external ReZygisk processes
remained present after the comparison. The evidence therefore does not attribute intermittent
no-start behavior to those modules or to the final debug backend. BlueStacks host logs instead
recorded clean player exits on guest reboot and separate launch failures caused by a surviving
bundled ADB server/player-lock handoff. Killing that exact server allowed the unchanged instance to
launch. A later restore of the unchanged known-good APK reproduced another host-side failure:
BlueStacks logged `launch_start_timeout`, a failing `qemu-img info`, and its own forced power-down
after 90 seconds. The rollback boot reached Launcher/SystemUI, and retrying the identical known-good
APK immediately passed initialization and cold boot. This strongly identifies the observed
intermittent case as BlueStacks lifecycle/storage handling rather than a candidate-specific Magisk
or module bootloop; it cannot exclude every future Android-side failure. This qualifies the normal
root/backend adapter and immutable-layout rejection for this exact instance; it does not qualify
persistent BlueStacks System Mode.

## Fault and property lanes

The host suite includes deterministic mount/doctor mutations, update URL and redirect properties,
strict module metadata, SQLite opcode interruption, abrupt process death, database-full, and
read-only recovery. Run all host checks with:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Each finding belongs in `security/findings/` with the affected version, preconditions, impact,
minimal proof, fix/adapter, regression, recovery path, and retirement condition. Findings on lab
targets feed this fork directly; external disclosure is optional and does not replace a local fix.

## Known advisory decisions

- `cxx` remains affected in both the frozen core and v30.7. No production source calls the affected
  macro, but PR6 must forward-port the official 1.0.195 fix plus Magisk's six-file fork delta before
  System Mode is ported. An unreachable local-only submodule commit is forbidden.
- The old `rand` finding is absent from v30.7 and its triggering custom-logger/thread-RNG feature
  combination is not enabled here; the old graph is retired instead of modernized twice.
- The RSA timing advisory has no patched version. Magiskboot signing is a local process rather than a
  network signing oracle. The advisory stays visible, and every crypto change reruns sign/verify.
- The historical global manager-signature bypass is fixed in the current hardening candidate:
  release builds enforce the embedded manager/stub certificate contract, while debug builds retain
  an explicit diagnostic relaxation. The exact live release manager and backend used the same
  external lab signer. Cross-signer lab transitions required an explicit uninstall/reinstall, so
  production-key migration and dynamic hidden-manager replacement remain dedicated scenarios.
- The former repository signing certificate is publicly recoverable from project history and was
  used by existing comparison artifacts. It is not a production identity. A candidate release must
  use an external persistent key and document the Android/daemon trust migration from existing
  installs before it can be promoted.
