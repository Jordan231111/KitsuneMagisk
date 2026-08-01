![](docs/images/logo.png#gh-dark-mode-only)
![](docs/images/logo_dark.png#gh-light-mode-only)

# KitsuneMagisk

KitsuneMagisk is an unofficial Magisk fork for Android 6.0 and newer. Its main
fork-specific feature is **Direct-System/System Mode** for controlled Android
environments where a normal boot-image installation is unavailable or impractical.

This project is not affiliated with or supported by
[topjohnwu/Magisk](https://github.com/topjohnwu/Magisk). Please do not send
Kitsune-specific support requests to the official Magisk project.

## Features

- **MagiskSU** — root access with app policy management
- **Magisk Modules** — systemless Android customization
- **MagiskBoot** and **MagiskPolicy** — boot-image and SELinux policy tools
- **Direct-System/System Mode** — persistent installation on explicitly qualified,
  writable emulator or device layouts
- **Kitsune hiding modes** — MagiskHide/DenyList/SuList behavior retained where it is
  tested and compatible

## Project status

The resumed project has not published its first production release. Current APKs are
development/test builds while installer recovery, signing migration, update ownership,
and exact emulator/device support are qualified.

System Mode can modify persistent system, init, and policy files. Use a disposable
emulator or a device with a verified restore path. Support is recorded for exact
Android images and emulator/device versions; a successful build alone is not a support
claim.

See the [current status](docs/status.md), [development roadmap](DEVELOPMENT_ROADMAP.md),
and [release notes](docs/release-notes.md) for details.

## Building

Requirements and platform setup are documented in [docs/build.md](docs/build.md).

```sh
git submodule update --init --recursive
./build.py ndk
./build.py all
```

Debug builds use the normal Android debug key. A release build uses one private,
long-lived maintainer keystore configured in the ignored `config.prop`, or in a
custom config file:

```sh
./build.py -r all
# or: ./build.py -r -c /path/to/release.prop all
```

The config format is shown in [config.prop.sample](config.prop.sample). Private signing
material must never be committed to the repository.

## Documentation

- [Installation background](docs/install.md)
- [Building and development](docs/build.md)
- [Current support and limitations](docs/status.md)
- [Development roadmap](DEVELOPMENT_ROADMAP.md)
- [Internal details](docs/details.md)

## Bug reports and contributions

Use a debug build when reporting a reproducible issue. Include install logs for install
failures and logcat or kernel/Magisk logs for runtime failures. Never attach private
keys, account credentials, or device secrets.

Translations live under each module's `src/main/res/values-*` directory.

## License

KitsuneMagisk and its submodules are free software under the GNU General Public
License, version 3 or later. See [LICENSE](LICENSE).
