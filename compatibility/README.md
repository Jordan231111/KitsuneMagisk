# System Mode compatibility evidence

`records/` contains sanitized observations conforming to the versioned characterization schema.
Synthetic classifier fixtures live under `tools/system_mode/fixtures` and are never support claims.

The initial required lanes are LDPlayer, MuMu, Nox, BlueStacks, and one immutable negative image.
PR3 recorded the connected MuMu 12 environment without mutating it. PR4A also recorded fresh
Android Studio API 35 (16 KiB pages) and API 36 immutable images as real fail-closed negative
evidence. LDPlayer, Nox, and BlueStacks remain `not_run`; they must not be converted to
`supported` from brand strings or synthetic data.

Support requires a clean snapshot plus install, three cold boots, upgrade, reinstall, module/root
smoke, uninstall, and snapshot restore. The current records intentionally preserve `not_run` for
every lifecycle operation that was not actually exercised.
