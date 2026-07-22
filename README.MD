# KitsuneMagisk

> **Experimental active-development project. There is currently no generally supported stable release.**

KitsuneMagisk is an unofficial Magisk fork whose primary product goal is persistent
**Direct-System/System Mode** installation for Android environments where normal boot-image
installation is unavailable or impractical. The main targets are commercial Android emulators and
selected real devices with a recoverable, persistently writable system layout.

This project is not affiliated with or supported by
[topjohnwu/Magisk](https://github.com/topjohnwu/Magisk). Do not ask the official Magisk project to
support Kitsune-specific behavior.

## What Kitsune is for

System Mode is the primary differentiator, but Kitsune remains a complete Magisk-derived root and
systemless-customization platform. It should preserve normal authorized boot/`init_boot`/
`vendor_boot` installation, superuser policy, modules and boot scripts, BusyBox, `resetprop`,
`magiskboot`, `magiskpolicy`, manager recovery, and safe uninstall. Its secondary fork features are
measured MagiskHide/DenyList/SuList behavior, hidden-manager recovery, SELinux-disabled compatibility,
and a versioned external-Zygisk boundary.

The project also maintains capability diagnostics, exact emulator/device qualification, upstream
change tracking, and an authorized disposable lab for fuzzing, vulnerability discovery, and
hardening. It does not promise to root arbitrary locked devices without a bootstrap or controlled
image, bypass every detector/attestation service, or support an architecture merely because its
binaries compile. See the roadmap's product-purpose hierarchy and evidence gates for the complete
contract.

## Current status

Development has resumed, but the existing `kitsune` branch is a reference implementation—not a
qualified claim of support for every emulator.

- The project currently builds and passes the existing normal Magisk AVD smoke flow on API 23, 29,
  and 35.
- Direct-System exists and has real emulator-specific history, but it does not yet have a dedicated
  automated install/cold-boot/upgrade/uninstall test.
- Support will be published for exact emulator versions and Android images only after repeatable
  qualification. “Works on one emulator” is useful evidence, not a universal compatibility claim.
- The current `31.0-kitsune` label is a compatibility/Android upgrade number inherited from earlier
  maintainers. It does not mean this tree contains a newer Magisk core than official Magisk.

See [the purpose and faithfulness audit](docs/faithfulness-audit.md) for the history-backed product
report, [the current support statement](docs/status.md) for the exact limitations, and
[the development roadmap](DEVELOPMENT_ROADMAP.md) for the audited implementation plan.

## Important current limitations

- Treat every existing APK as experimental. Automatic stable publication from every `kitsune`
  commit is disabled by the current CI design.
- Built-in app and stub updates now fail closed with an explicit message; the dead prior-maintainer
  endpoints are no longer contacted. Install test builds manually from this repository's Actions
  artifacts or explicitly labeled canary releases until PR10 adds a project-owned update service.
- Published artifacts through `25fa2159` converged normal hide selections on `denylist` without an
  upgrade migration. Current source adds transactional database migration v13, preserves a verified
  version-12 backup and the legacy table, and tests divergent/SuList fixtures; published older APKs
  remain subject to the original caveat.
- Debug native builds automatically grant ADB shell root during the current test flow. Debug APKs
  are development artifacts and must not be described as ordinary stable releases.
- ReZygisk, NeoZygisk, DenyList, and SuList compatibility is provider- and version-specific. No
  blanket external-Zygisk or current SuList guarantee is made.
- Direct-System modifies persistent Android partitions and boot-time policy/init files. Use a
  disposable emulator instance or a recoverable device snapshot until the transactional installer
  and rollback gates in the roadmap are complete.

## Development branches

- `kitsune` remains the known-working comparison branch. It keeps its name and receives only tests
  or fixes needed by another build that will actually be released.
- `next-system` will start from the latest audited official stable (currently Magisk v30.7 after a
  branch-cut recheck) and forward-port System Mode first.
- `next-system` becomes the new `kitsune` only after it meets or beats the current branch on the same
  emulator/device parity matrix. The old implementation is then retained as a tag, not maintained as
  a second product.

## How to contribute

Use the **Proposed pull-request sequence** in [DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md) as the
execution order. The P0/P1/P2 sections are the detailed requirements, evidence, test cases, and
acceptance criteria used while implementing those PRs; they are not a competing second task list.

For the current development phase:

1. Keep System Mode as the product gate.
2. Add a failing test or reproducible emulator case before changing boot/init/SELinux behavior.
3. Keep current-branch fixes conditional; do not modernize the old and new cores in parallel.
4. Run the same evidence-driven checks on current `kitsune` and `next-system` before promotion.

## Building

Requirements and platform details are in [docs/build.md](docs/build.md). The short path is:

```sh
export ANDROID_SDK_ROOT=/path/to/android-sdk
git submodule update --init --recursive
./build.py ndk
./build.py all       # debug APKs and native binaries
./build.py -r all    # release variant for testing
```

Build outputs are written under `out/`. A clean checkout should use `config.prop.sample` as the
configuration reference.

## Documentation

- [Current support and known limitations](docs/status.md)
- [Purpose, history, install-route, and faithfulness audit](docs/faithfulness-audit.md)
- [Development roadmap and commit audit](DEVELOPMENT_ROADMAP.md)
- [Building and development](docs/build.md)
- [Installation background](docs/install.md)
- [Internal details](docs/details.md)
- [GPLv3 license](LICENSE)

## License

KitsuneMagisk and its submodules remain free software under the GNU General Public License, version
3 or later. See [LICENSE](LICENSE).
