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

The RustSec check always audits against the live advisory database and fails on any unreviewed or
changed finding. The generated report retains the database commit and advisory count as historical
evidence, but metadata-only database updates do not stale an otherwise identical product report.
Each observed finding fingerprints its advisory, affected-target, and version-range record so a
relevant metadata change or newly published fix still requires review.

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

## Evidence records

Generated ledgers under `security/generated/` are review inputs, not release attestations. Candidate
artifact reports and device-corpus JSON belong in the ignored `out/` tree or other disposable lab
storage. Exact historical runs, artifact hashes, emulator images, observed failures, and the limits
of each result are maintained in [`DEVELOPMENT_ROADMAP.md`](../DEVELOPMENT_ROADMAP.md); duplicating
that volatile evidence here previously made this runbook stale and difficult to review.

An AVD or commercial-emulator pass proves only the named image and lifecycle. Compilation for an ABI
does not prove runtime support, an ordinary Magisk root pass does not prove System Mode, and a
reproduced vendor launcher failure does not exclude every possible guest-side boot failure.

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
