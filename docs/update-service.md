# Signed manager and stub updates

The manager and hidden stub verify updates against the project signing certificate.
Beta and canary feeds are active; stable promotion remains blocked by the [release gates](release.md).
The [Beta 4 acceptance record](release-pr16.json) identifies the published APKs and clean-stub test.

## Ownership and identity

The current package is `io.github.jordan231111.magisk`. Its feeds are:

```
https://raw.githubusercontent.com/Jordan231111/KitsuneMagisk/update-feed/v1/io.github.jordan231111.magisk/beta.json
https://raw.githubusercontent.com/Jordan231111/KitsuneMagisk/update-feed/v1/io.github.jordan231111.magisk/canary.json
```

The previous `io.github.huskydg.magisk.next` package retains its separate files directly under
`v1/`. Never replace an old-package feed with a new-package manifest.
See the [package migration](build-info.md#pr15-namespace-migration).

The experimental default is `beta`; stable promotion stays blocked by the [production gates](release.md). Existing installs
remain opted out of background checks. Custom URLs must use HTTPS and end in `beta.json`, `stable.json` or
`canary.json`, and retain the same pinned project signature. There is no unsigned JSON fallback or
public debug update channel. Old Debug selections resolve to the default beta channel,
without changing signer trust or enabling background checks. The full manager retains its DNS-over-HTTPS preference.

One external RSA-3072 keystore signs both production APKs and metadata. The private keystore and
four-field build config are outside the checkout, under `$HOME/.config/kitsune-magisk/signing/`.
They are mode 0600 in a mode-0700 directory. Release APK builds require all four external
signing fields and reject in-checkout configs/keystores and the Android debug-key alias. Passwords are never command-line arguments, repository
files, CI secrets added by this change, logs or APK assets. The public certificate SHA-256 is:

```
9b09eb317c96036185884700d9584e63cc4a2a288825a0172633ba7bfe222ec6
```

`updates/trust.json` contains only public trust data. Every manager/stub also contains its actual
build signer's public certificate. Repackaging a hidden stub preserves that original-manager pin.
Debug and disposable CI builds cannot silently update to another signer. Historical public-key
installations and engineering `.next` installations signed with the debug key require an explicit
backup/reinstall transition before production adoption. No public historical key or signature
bypass is restored. The owner must retain independent protected backups of the production key and
password before production promotion; another folder on the same disk is not disaster recovery.

## Wire contract

An envelope has exactly `schema`, `key_id`, `payload`, and `signature`. `payload` is canonical base64
of the UTF-8 JSON bytes. The RSA PKCS#1 SHA-256 signature covers the domain separator
`KitsuneMagisk update v1\n` followed by those exact bytes. Clients reject duplicate/unknown keys,
malformed UTF-8, noncanonical base64, invalid numbers, excess nesting and oversized input.

The signed payload separates its distribution channel (`beta`/`stable`/`canary`) from the APK
build channel (`next-system-experimental`). It records monotonic sequence, issue/expiry times, product and Android
versions, minimum Android API, package, exact source/upstream commits, dirty state, build ID, protocol,
variant, signer, inline release/migration notes, release tag, and separate manager/stub URL/size/SHA-256
descriptors. The publisher derives the minimum API from both APK manifests; clients reject unsupported
Android versions before downloading, and API 24+ also compares the parsed manifest requirement.
Metadata is limited to 128 KiB, manager APKs to 200 MiB and stubs to 8 MiB. Validity is at most
30 days (14 by default). Public publication tooling rejects dirty sources and debug artifacts.
QA fixtures are signed test inputs in ignored `out/`, explicitly separate from feed preparation.

Rollback protection persists accepted sequence and payload hash per channel, the highest accepted
Android code and its APK hash across channels, and the latest valid observed time. Switching channels
does not reset the floor. The same sequence cannot change content; the same app code cannot identify
different APK bytes. A clock rollback beyond five minutes, expired metadata or corrupt saved state
fails closed. Refreshing a channel requires a higher sequence; the publisher can authenticate expired
old metadata to preserve its floor. Clearing app data/root-controlled rollback is outside the remote
replay boundary; the installed/embedded app-code floor and expiry still apply.

All network operations are bounded and run off the manager UI thread. Metadata reads have a
60-second transfer budget and APK reads a ten-minute budget, in addition to bounded connection/read
timeouts; slow trickles cannot hold a downloader indefinitely. Metadata redirects stay on
the selected HTTPS host. APK descriptors must name this repository's release/tag, and redirects are
limited to GitHub and its release-asset host. Credentials, plaintext redirects, unexpected encoding
and excessive redirect chains are rejected. Streams are length/hash checked before any installation.
Stopped jobs/services cancel their downloads, interrupted APK writes abandon the installer session,
and unsuccessful metadata checks still finish their scheduler job.

The platform verifies the APK signature and exact signer/package/version; the APK's build info and
embedded stub must also match the signed record. Files are written to private temporary paths,
synced, verified, made read-only and atomically renamed. Failed downloads retain the current manager.
Unfinished `.part` files are never loadable. Hidden-manager current/update/development/seed APKs are
verified before creating a classloader, independently of whether they arrived through this client.
This uses the platform's certificate collection with verification enabled, as in
[AOSP PackageManager](https://android.googlesource.com/platform/frameworks/base/+/4be0bf277e19be48f68bb50904ecbd9e872c751e/core/java/android/content/pm/PackageManager.java).
API 28+ requests both signature flags: [Android 9-10's archive parser](https://android.googlesource.com/platform/frameworks/base/+/android-10.0.0_r47/core/java/android/content/pm/PackageManager.java)
requires the legacy flag to collect and verify certificates even when modern signing information is requested.

## Publication and verification

Use the [release guide](release.md) to prepare, qualify and publish a candidate.
Publish the exact tested APKs before their signed feed, preserve the channel sequence floor,
and keep production and debug signing identities separate.

Verify the actual public downloads:

```sh
scripts/env.py python3 tools/update_service.py verify-published \
  --url https://raw.githubusercontent.com/Jordan231111/KitsuneMagisk/update-feed/v1/io.github.jordan231111.magisk/beta.json \
  --channel beta --output out/published-update-verification
```

If the prerelease includes `app-debug.apk`, supply `--debug-apk-sha256` with its tested digest.
The verifier rejects extra APKs unless explicitly pinned. Then test a clean production stub
and confirm that its installed manager matches the published APK.

CI uses a disposable signer. `tools/update_service.py test-fixtures --config /path/to/config.prop`
prepares failure cases for `UpdateServiceTest`; it does not need the production key.
MuMu cold boots require full CLI close/open, never `adb reboot`.

## Historical distribution and remaining gates

Original Full/Lite and moved-tag release provenance remains in the PR8 ledger. Their dead/unsigned
endpoints are not trusted redirects into the new feed. A matching old version label is not an
authenticated upgrade path. The [historical ledger](port-ledger.md), [Hide/SuList contract](hide-policy.md)
and [module contract](module-contract.md) describe retained behavior and migration. Keep an existing
qualified setup where the replacement target remains unqualified.
The [historical module template](port-history/module-template.json) retains its exact 8,537-byte
digest and immutable archive locator. It is an archival reference for the old module API, not an
advertised installer/template for the new runtime. The [module contract](module-contract.md) records
that API's tested migration.

The [historical PR10 acceptance](update-service-acceptance.json)
passed public-feed/asset verification and the clean-stub and manager update flows on MuMu VM 1.
Independent protected key backup remains a production-promotion requirement under P0.7/PR16.
This is not proof of physical-phone root qualification.

## Acceptance evidence

The [PR10 acceptance record](update-service-acceptance.json) retains the original package's
public-download, clean-stub, manager-update and signer checks. The
[Beta 4 record](release-pr16.json) covers the current package and published bytes, including
production-stub installation and original MuMu state restoration. These results do not qualify
physical-phone root or release-APK System Mode.
