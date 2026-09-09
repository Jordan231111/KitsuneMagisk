![](docs/images/logo.png)

# KitsuneMagisk Next

This branch is the experimental, maintainable-base rebuild of KitsuneMagisk. It is based directly
on official Magisk v31.0 prerelease and carries the experimental Direct-System implementation
and qualification tools. PR7A qualification is in progress; see the [roadmap](DEVELOPMENT_ROADMAP.md).

It installs as `io.github.huskydg.magisk.next`, reports a
`31.0-kitsune-next.<source>` version, and uses the `next-system-experimental` channel identity. It
does not replace official Magisk or the existing Kitsune manager. Automatic update checks default
to off because this branch does not yet have a project-owned update endpoint.

## Important status

- This is an unofficial engineering baseline, not a production release or a daily-phone build.
- Direct-System/System Mode—the primary purpose of KitsuneMagisk—is available only in debug builds
  after machine qualification of the exact writable target and its external recovery copy. See
  [the qualification guide](docs/system-mode/pr7-machine-qualification.md).
- MagiskHide/SuList, early-mount, and external-Zygisk integration are also not present yet.
- Normal upstream Select-and-Patch, Direct Install, inactive-slot, recovery, emulator live setup,
  MagiskSU, modules, and built-in Zygisk remain in scope and must stay green.

The exact upstream commit, submodule pins, toolchain, advisories, reviewed source delta, and local
qualification evidence are recorded in
[`docs/next-system-baseline.md`](docs/next-system-baseline.md). The current machine-readable contract is
[`docs/next-system-pr7a.json`](docs/next-system-pr7a.json).

## Building

Follow the upstream [Magisk build documentation](https://topjohnwu.github.io/Magisk/build.html),
then run:

```sh
./build.py -v all
./build.py -vr -c /path/to/external-signing-config.prop all
python3 tools/next_system_baseline.py --manifest docs/next-system-pr7a.json --source --artifacts out
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
