# Machine-readable security evidence

- `upstream-policy.json` owns sensitive path classification and stable/master/fork dispositions.
- `dependency-policy.json` records dependency comparison decisions and license evidence hints.
- `rustsec-dispositions.json` accounts for every observed Cargo advisory without audit ignores.
- `submodule-policy.json` fail-closes on any historical gitlink loss not already reviewed.
- `trust-boundaries.json` maps privileged inputs to failure modes, controls, and lab targets.
- `signature-threat-model.json` defines normal and hidden-manager identity requirements.
- `architecture-evidence.json` prevents build, parser, AVD, System Mode, and physical recovery claims
  from being conflated.
- `generated/` contains deterministic ledgers and the SPDX snapshot.
- `findings.json` is the fail-visible finding index; `findings/` contains minimized proofs, fixes,
  regressions, recovery, and retirement conditions.

See [`docs/security-lab.md`](../docs/security-lab.md) for regeneration and device-lab commands.
