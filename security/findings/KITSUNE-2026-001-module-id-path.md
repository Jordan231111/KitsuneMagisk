# KITSUNE-2026-001 — module ID reached destructive paths before validation

- Status: fixed with a regression in PR4B
- Affected baseline: `f943ecddd11d0e648877ffb1f917c16767b8f7cc`
- Surface: `scripts/util_functions.sh` module installation
- Classification: privileged reliability/safety defect; not a new privilege escalation

## Finding

The module installer read `id` from `module.prop`, concatenated it into
`$NVBASE/modules_update/$MODID`, and passed the result to `rm -rf` before checking the documented
module-ID grammar. Empty IDs, separators, glob characters, or traversal components could therefore
select a path other than one new module directory.

Magisk modules are explicitly approved root code and can already execute arbitrary commands through
`customize.sh`, so this is not represented as a sandbox escape or independent elevation. It matters
because malformed metadata reached a destructive operation *before* custom code or a clear trust
decision, turning accidental/corrupt metadata into avoidable cross-module or filesystem damage.

## Minimal proof

No real deletion is needed. Before the fix, the path expression was mechanically:

```text
MODULEROOT=/data/adb/modules_update
MODID=../../local/tmp/example
MODPATH=$MODULEROOT/$MODID
```

The first mutation was `rm -rf $MODPATH`. The regression now sends the documented examples plus 750
seeded values through the actual shell validator and proves validation occurs before `MODPATH` is
assigned or removed.

## Fix and retirement condition

- Enforce the official `^[a-zA-Z][a-zA-Z0-9._-]+$` contract before path construction.
- Clear `MODPATH` before aborting an invalid module so cleanup cannot consume inherited state.
- Quote the first destructive path operations and the abort cleanup paths.
- Carry the regression to `next-system`; retire the fork patch only if a future official stable has
  equivalent pre-mutation validation and passes the same property corpus.

## Recovery and disclosure

The proof uses temporary host subprocesses only and changes no device. Affected users who suspect a
malformed module should restore their `/data/adb/modules*` backup or known-good emulator snapshot.
An optional upstream report may be useful, but Kitsune's tested fix is not gated on external action.
