# Release qualification

[PR16](https://github.com/Jordan231111/KitsuneMagisk/pull/44) is merged. The
[Beta 4 acceptance](release-pr16.json) records code 31021, immutable assets, public-feed verification
and a clean production-stub update. Stable promotion remains blocked by the gates below.

Builds use the maintained `next-system` branch. The current release gate is
[`next-system-pr16.json`](next-system-pr16.json). The upstream base remains official
Magisk v31.0 prerelease; v30.7 remains official stable as checked on 2026-10-18.

Stable promotion is blocked by [physical qualification](https://github.com/Jordan231111/KitsuneMagisk/issues/39),
[historical archive dispositions](https://github.com/Jordan231111/KitsuneMagisk/issues/40), and
[independent signing-key recovery](https://github.com/Jordan231111/KitsuneMagisk/issues/41).
System Mode is available in both variants from code 31022. The release matrix must exercise
the actual production-signed APK; debug tests alone do not qualify it. The implementation and existing qualification
records remain available. Candidate releases claim only their exact tested targets.

## Prepare a candidate

1. Increase the Android upgrade code in `kitsune-build.json`, review the source and commit it.
   Preserve the compatibility epoch and protocol unless their contracts change.
2. Build both variants with `./build.py all` and
   `./build.py -r -c /external/production.prop all`. Release signing requires the existing external
   production key. CI's disposable signer is only for testing. Cargo uses the committed lockfiles.
3. Run the host suite, both Clippy profiles, app lint and the full PR workflow. Run the local
   matrix in `release-policy.json`, including 4/16 KiB AVDs and MuMu Device-1. Preserve the exact
   APKs, test logs and `kitsune.device-qualification.v1` records. MuMu requires full CLI close/open,
   current-format upgrade/data preservation, vendor-root-off checks, uninstall and original-image restore.
   Set `AVD_STRESS_ITERATIONS=8` and `AVD_STRESS_PARALLEL=6` for 48 root requests per variant;
   the concurrency-only runs also execute `MagiskAppTest#testSuRequest` to authorize the lab shell.
4. Audit all three owned Rust lockfiles with `scripts/env.py cargo audit --file PATH --json`.
   Retain the raw reports and current lockfile digests. The only accepted finding is
   [RUSTSEC-2023-0071](https://rustsec.org/advisories/RUSTSEC-2023-0071.html) for native `rsa`:
   local boot signing still has a timing limitation, tracked in
   [#42](https://github.com/Jordan231111/KitsuneMagisk/issues/42). New findings fail qualification.
5. Assemble `acceptance.json` beside its evidence files. Schema `1` includes `source_commit`,
   `ci_run`, `checks` (`host`, `clippy`, `lint`, `build-debug`, `build-release`), `runtime` keyed by
   the policy's target IDs, `mumu_lifecycle`, and `audits` keyed by the three Cargo.lock paths.
   Every file reference has a relative `path` and `sha256`. Checks include `command`, `exit_code`
   and `log`; audits include `lock_sha256`, UTC `checked_at` and `report`. The MuMu lifecycle
   record identifies the source/APK and each required install/restore outcome. Preserve actual
   results; a success flag is a maintainer attestation, not a substitute for the linked logs.

```sh
scripts/release.sh verify --acceptance /path/to/evidence/acceptance.json
scripts/release.sh inventory
scripts/env.py python3 tools/update_service.py prepare \
  --config /external/production.prop --qualification out/release-info.json \
  --channel beta --sequence NEXT_SEQUENCE --notes /path/to/release-notes.md \
  --output /path/to/feed/beta.json
```

The receipt binds the exact clean source, APK hashes, production certificate, runtime records,
current audits and passing CI jobs. Qualification expires after seven days. The Rust CycloneDX
inventory covers native/build-tool dependencies; the source contract records native submodules
and toolchain pins. It is not a complete Android or operating-system SBOM.

## Publish the tested bytes

Create a draft GitHub prerelease at the receipt's source/tag. Attach only the tested manager,
matching stub, checksums, release receipt, Rust inventory and reviewed notes. An explicitly tested
debug manager may accompany an experimental release for diagnostics; it uses a different signer
and never enters the update feed. Exclude instrumentation APKs, private signing files and fault fixtures.

Publish the immutable release, then update the package-specific channel on `update-feed`, preserving
the previous sequence floor. Do not rebuild, replace assets or overwrite an old-package feed.
Use `tools/update_service.py verify-published` and a clean production stub to check public downloads,
signer, embedded stub and manager startup. See [the update protocol](update-service.md).

Historical-public-key, debug-key and previous-package installations require backup and explicit
reinstallation as described in [namespace migration](build-info.md#pr15-namespace-migration).
Never disable certificate checks to make an upgrade succeed.

Stable metadata additionally requires an empty production-blocker list, a passing complete historical
ledger and a final semantic product version. Closing an issue alone does not qualify a target.
Known intermittent lab failures remain tracked in [#43](https://github.com/Jordan231111/KitsuneMagisk/issues/43).

The Android 16.1 acceptance used an explicit temporary `-kernel` override matching the official
PR15 kernel digest. The installed SDK had a different custom kernel and was preserved. Bind
SDK inputs before testing; the qualification archive contains the actual harness and kernel record.
