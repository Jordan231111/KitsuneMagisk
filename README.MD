![](docs/images/logo.png)

# KitsuneMagisk Next

Maintained by [Jordan231111](https://github.com/Jordan231111).

This branch is the experimental, maintainable-base rebuild of KitsuneMagisk. It is based directly
on official Magisk v31.0 prerelease and carries the experimental Direct-System implementation
and qualification tools. PR7A passed its prepared-MuMu and ordinary emulator gates. PR8's
[branch decision](docs/pr8-branch-decision.md) selects this development foundation; historical
feature completion and production qualification remain gated by the [roadmap](DEVELOPMENT_ROADMAP.md).

The manager installs as `io.github.jordan231111.magisk`, reports a
`0.1.0-dev-kitsune.<source>` version, and uses the `next-system-experimental` channel identity. It
uses a separate package from official Magisk and the prior Kitsune manager. Automatic update checks remain off during
qualification. The previous namespace has an [authenticated experimental beta](docs/update-service.md);
the new namespace uses a separate feed and requires a [one-time migration](docs/build-info.md#pr15-namespace-migration).
Production promotion remains gated.

## Important status

- This is an unofficial engineering baseline, not a production release or a daily-phone build.
- Direct-System/System Mode is one Kitsune installation capability, available only in debug builds
  after machine qualification of the exact writable target and its external recovery copy. See
  [the qualification guide](docs/system-mode/pr7-machine-qualification.md).
- [Hide/SuList core and migration](docs/hide-policy.md) are implemented and qualified on the
  recorded emulator tuples, with [manager integration](docs/manager-contract.md) and
  [versioned module APIs](docs/module-contract.md). PR15 owns broader device qualification.
  The [module compatibility matrix](docs/module-compatibility.md) records pinned third-party
  modules, working configurations, official Magisk controls, and explicit limitations.
  [The provider decision](docs/adr/0001-zygisk-provider.md) retains maintained built-in Zygisk
  and an explicit, versioned external adapter boundary.
- Normal upstream Select-and-Patch, Direct Install, inactive-slot, recovery, emulator live setup,
  MagiskSU, modules, and built-in Zygisk remain in scope and must stay green.

The exact upstream commit, submodule pins, toolchain, advisories, reviewed source delta, and local
PR6 qualification evidence are recorded in
[`docs/next-system-baseline.md`](docs/next-system-baseline.md). The current machine-readable contract is
[`docs/next-system-pr15.json`](docs/next-system-pr15.json), inheriting PR7A's runtime baseline.

## Building

Follow this branch's [build and development guide](docs/build.md), then run:

```sh
./build.py -v all
./build.py -vr -c /path/to/external-signing-config.prop all
python3 tools/next_system_baseline.py --manifest docs/next-system-pr15.json --source --artifacts out
```

A release test build must use an external, disposable or production-controlled signing key. The
repository contains no Kitsune release key and the PR6 gate rejects the public Android debug key as
a release identity.

## Upstream project

Magisk and its original documentation are maintained by
[topjohnwu](https://github.com/topjohnwu/Magisk). KitsuneMagisk is an independent fork and must not
send fork-specific bug reports to the upstream project.

## License

Magisk, including all git submodules, is free software distributed under the GNU General Public
License, version 3 or (at your option) any later version. See [LICENSE](LICENSE).
