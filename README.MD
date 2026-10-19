![](docs/images/logo.png)

# KitsuneMagisk Next

Maintained by [Jordan231111](https://github.com/Jordan231111).

KitsuneMagisk provides Magisk root with Kitsune Hide/SuList, early mounting, module compatibility
and System Mode for qualified writable targets. The maintained source is based on official Magisk
v31.0 prerelease with selected later fixes. It does not include every change on upstream master;
[follow-up integration](https://github.com/Jordan231111/KitsuneMagisk/issues/49) is tracked separately.
Ordinary boot-image installation and recovery remain central to the project.

The current [Release 31023](https://github.com/Jordan231111/KitsuneMagisk/releases/tag/kitsune-canary-dd5ead427f5ab028787e65312f7aa6fb9b8b6760)
is GitHub’s latest release. Its [acceptance record](docs/release-pr16.json) identifies the tested APKs and
follow-up work. [Compatibility records](compatibility/TESTED.md) retain their exact tested versions;
virtual devices do not qualify physical phones. See the [roadmap](DEVELOPMENT_ROADMAP.md) for status.

The manager package is `io.github.jordan231111.magisk`. Builds report their product version,
source commit and upstream base separately. [Signed updates](docs/update-service.md) use the
project certificate; historical, debug and previous-package installations require the documented
[backup/reinstall transition](docs/build-info.md#pr15-namespace-migration).

- [Installation guide](docs/install.md).
- [Shamiko installation and the tested workaround](https://github.com/Jordan231111/KitsuneMagisk/issues/48).
- [Hide/SuList](docs/hide-policy.md) and [manager/SU controls](docs/manager-contract.md).
- [Module APIs](docs/module-contract.md), [tested third-party modules](docs/module-compatibility.md)
  and [Zygisk providers](docs/adr/0001-zygisk-provider.md).
- [System Mode qualification](docs/system-mode/pr7-machine-qualification.md): release and debug
  builds from code 31023, with an exact writable target and verified external recovery copy.
- [Current source and artifact contract](docs/next-system-pr16.json).

## Building

Follow this branch's [build and development guide](docs/build.md), then run:

```sh
./build.py -v all
./build.py -vr -c /path/to/external-signing-config.prop all
python3 tools/next_system_baseline.py --manifest docs/next-system-pr16.json --source --artifacts out
```

A release test build must use an external, disposable or production-controlled signing key. The
repository contains no Kitsune release key and the artifact gate rejects the public Android debug key as
a release identity.

## Upstream project

Magisk and its original documentation are maintained by
[topjohnwu](https://github.com/topjohnwu/Magisk). KitsuneMagisk is an independent fork and must not
send fork-specific bug reports to the upstream project.

## License

Magisk, including all git submodules, is free software distributed under the GNU General Public
License, version 3 or (at your option) any later version. See [LICENSE](LICENSE).
