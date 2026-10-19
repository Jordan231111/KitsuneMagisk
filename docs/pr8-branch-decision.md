# Development branch decision

PR8 selected the official Magisk v31.0-based `next-system` foundation on 2026-10-08.
It is now the repository's default branch. The previous line is preserved at
`archive/kitsune`; it is no longer a second maintained branch.

The selected foundation was `dd5ca5c12e8447344eef5e89f6080e9e2a0d04c8`, based on official
`96221b69fae9910b1c0c75c2f92a4ebb2c2dc698`. PR8 made a development decision, not a stable-release
or physical-phone support claim. Current results are in the [release record](release-pr16.json).

## Evidence

- The [Windows comparison](system-mode/pr8-windows-2026-10-08.md) records the exact LDPlayer,
  MuMu and BlueStacks images, APKs, failures and restores.
- The [Mac MuMu record](system-mode/mumu-pr7-2026-09-09.md) and
  [PR7A CI matrix](https://github.com/Jordan231111/KitsuneMagisk/actions/runs/34476924390)
  cover their stated ordinary and System Mode routes.
- The [fork delta](pr8-fork-delta.json) measures changes against each line's upstream base.
  Different bases make file counts unsuitable as a feature-parity score.
- The [feature ledger](port-ledger.md) records retained behavior, replacements, retirements and
  unavailable targets. Historical releases and archive gaps remain individually identified.

## What the decision preserves

Use maintained upstream boot, init, policy, resetprop and SU implementations. Preserve Kitsune
Hide/SuList, early mounting, module APIs, provider choice and emulator installation through their
tested contracts. System Mode complements ordinary boot-image installation.

Do not restore public production keys, signature bypasses or implicit filename-based privilege
to reproduce old implementation details. Older installations need their documented migration
or recovery procedure.

Current release-APK System Mode remains [unfinished](https://github.com/Jordan231111/KitsuneMagisk/issues/47).
Stable promotion also requires physical-device results and the remaining
[release gates](release.md). Historical tests cannot establish support for new APKs or targets.
