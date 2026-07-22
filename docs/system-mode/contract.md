# System Mode characterization contract v1

This contract is the portable comparison surface for current `kitsune` and the future
`next-system` branch. It does not install, remount, patch policy, or create a persistence marker.
Any result that needs a mutation, cold boot, host restart, or snapshot restore remains unproven
unless the caller supplies that evidence explicitly.

## Run the doctor

From a connected target:

```sh
python3 tools/system_mode/kitsune.py system-mode doctor \
  --serial 127.0.0.1:16384 --json
```

To connect to the project lab port first:

```sh
python3 tools/system_mode/kitsune.py system-mode doctor --connect 16384 --json
```

The command returns `0` only for `supported`, `3` for a valid `blocked` or `needs_evidence`
assessment, and `2` when collection itself fails. Public records hash the ADB serial, build
fingerprint, and boot ID. Raw identifiers are never required for comparison.

The JSON Schema is
[`tools/system_mode/schemas/doctor-v1.schema.json`](../../tools/system_mode/schemas/doctor-v1.schema.json).
Stable reason-code meanings are in
[`tools/system_mode/contracts/reason-codes.json`](../../tools/system_mode/contracts/reason-codes.json).

## Safety boundary

The collector reads:

- Android properties, kernel identity, ABI, and a hashed boot ID;
- `/proc/self/mountinfo` and the effective backing mount for the root/system partition family;
- when `/system` is ext4, the filesystem magic and read-only-compatible feature word from the
  documented superblock offsets (or `tune2fs -l` metadata when available);
- path existence, readability, and permission-level writability for init and SELinux candidates;
- SELinux state, existing System Mode config/manifest presence, and staging free space;
- root transport identity via `id` and, when ADB is not root, one `su -c id` probe.

It does not equate `test -w`, an `rw` mount, root ADB, or an overlay with persistent writability.
Only a controlled marker that survives a cold boot can satisfy `PERSISTENT_WRITABILITY_UNPROVEN`.
The marker workflow is deliberately outside this read-only command and must run on a disposable
snapshot.

Invoking `su -c id` may create the root implementation's ordinary authorization/audit entry. It
does not modify the system image. Use a root-ADB image if even that audit side effect is unsuitable.

## Evidence inputs

The following flags record evidence created by an external lab workflow; they do not perform or
invent the evidence:

- `--init-import-proven`: a harmless marker RC was parsed on a disposable snapshot;
- `--snapshot-id`, `--backup-digest`, `--restore-command`, and `--recovery-verified`: one complete,
  exercised recovery tuple;
- `--backing-write-probe passed --cold-boots N`: a removed controlled marker survived at least
  three cold boots;
- `--host-restarts N`: host lifecycle evidence, recorded separately from Android cold boots.

The classifier will not return `supported` when any required tuple is incomplete.

## Install state machine and manifest

The normative state graph is
[`install-state-machine.json`](../../tools/system_mode/contracts/install-state-machine.json).
Persistent installer work in later PRs must never skip from `UNINSTALLED` to `COMMITTED`:

```text
UNINSTALLED -> PREFLIGHTED -> STAGED -> COMMITTED -> BOOT_VERIFIED
                                  \-> ROLLBACK_REQUIRED -> ROLLING_BACK
```

`FAILED` means automatic recovery cannot prove either the original state or the prior installed
state; the external restore path is mandatory. A retry is forbidden while a journal is in
`ROLLBACK_REQUIRED`, `ROLLING_BACK`, or `FAILED`.

The future on-device ownership record must validate against
[`install-manifest-v1.schema.json`](../../tools/system_mode/schemas/install-manifest-v1.schema.json).
It records full source identity, adapter, payload digests, exact original paths and metadata,
external backup, strategies, and a monotonically ordered mutation journal. Wildcard ownership is
not representable in the schema.

## Fixture and characterization formats

Classifier fixtures under [`tools/system_mode/fixtures`](../../tools/system_mode/fixtures) are
synthetic safety cases, not device-support claims. They cover writable LDPlayer/MuMu/Nox shapes,
a BlueStacks host-adapter requirement, and an immutable EROFS/AVB rejection.

Validate them with:

```sh
python3 tools/system_mode/kitsune.py system-mode validate-fixtures
```

Actual lab observations use
[`characterization-record-v1.schema.json`](../../tools/system_mode/schemas/characterization-record-v1.schema.json)
and live under `compatibility/records/`. A record must say whether the starting image was a clean
snapshot and must use `not_run` rather than inferring lifecycle success from a working root shell.
