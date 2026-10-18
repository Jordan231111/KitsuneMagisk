![](docs/images/logo.png)

# KitsuneMagisk Next

Maintained by [Jordan231111](https://github.com/Jordan231111).

KitsuneMagisk provides Magisk root with Kitsune Hide/SuList, early mounting, module compatibility
and System Mode for qualified writable targets. The maintained source is based on official Magisk
v31.0 prerelease. Ordinary boot-image installation and recovery remain central to the project.

This branch is experimental. [Tested targets](compatibility/TESTED.md) identify exact images and
limits; virtual-device tests do not qualify physical phones. See [release qualification](docs/release.md)
for the remaining production gates and the [roadmap](DEVELOPMENT_ROADMAP.md) for development status.

The manager package is `io.github.jordan231111.magisk`. Builds report their product version,
source commit and upstream base separately. [Signed updates](docs/update-service.md) use the
project certificate; historical, debug and previous-package installations require the documented
[backup/reinstall transition](docs/build-info.md#pr15-namespace-migration).

- [Hide/SuList](docs/hide-policy.md) and [manager/SU controls](docs/manager-contract.md).
- [Module APIs](docs/module-contract.md), [tested third-party modules](docs/module-compatibility.md)
  and [Zygisk providers](docs/adr/0001-zygisk-provider.md).
- [System Mode qualification](docs/system-mode/pr7-machine-qualification.md): debug builds only,
  with an exact writable target and verified external recovery copy.
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
