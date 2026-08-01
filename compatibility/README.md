# System Mode compatibility evidence

`records/` contains sanitized observations conforming to the versioned characterization schema.
Synthetic classifier fixtures live under `tools/system_mode/fixtures` and are never support claims.

The initial required lanes are LDPlayer, MuMu, Nox, BlueStacks, and one immutable negative image.
PR3 recorded the connected MuMu 12 environment without mutating it. PR5A/PR5B add an experimental
MuMuPlayer 1.4.46 lifecycle record backed by a verified external image restore; it remains
experimental because the starting image contained a legacy System Mode installation and the exact
final artifact did not repeat every predecessor module/failure-injection subcase. PR4A also recorded
fresh Android Studio API 35 (16 KiB pages) and API 36 immutable images as real fail-closed negative
evidence. LDPlayer and Nox remain `not_run`; no lane may be converted to `supported` from brand
strings or synthetic data.

Support requires a clean snapshot plus install, three cold boots, upgrade, reinstall, module/root
smoke, uninstall, and snapshot restore on the exact artifact. Records intentionally preserve
`not_run` and `experimental` wherever that exact claim was not exercised.
