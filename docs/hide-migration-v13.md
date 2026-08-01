# Hide-table compatibility reconciliation

This is an internal upgrade/rollback safeguard, not a new public database release.

A fresh KitsuneMagisk install has no HideList rows to move. It creates the current tables and an
internal completion marker directly, without creating a pointless pre-migration backup.

The compatibility path matters when an older Kitsune/Delta development build already stored
normal-hide selections in `hidelist`.

Commit `25fa2159` switched current normal-hide behavior to `denylist` without moving old rows. If an
existing database is opened without reconciliation, previous selections can silently disappear from
the active view. SuList is separate and remains in `sulist`.

## Conflict rule

The daemon uses a security-conservative union:

1. Keep every existing `denylist` row.
2. Add each non-empty `hidelist` package/process pair that is not already present.
3. Keep `hidelist` as the rollback/audit source.
4. Never copy, clear, or reinterpret `sulist` rows.

The database stays at `PRAGMA user_version=12`, which is the version understood by the older
daemon. The historical internal table name `hide_migration_v13` is retained only so databases made
by the short-lived development experiment can be recognized safely; it does not advertise schema
version 13.

Keeping version 12 prevents an older daemon from treating the database as an unsupported downgrade
and deleting/rebuilding it. It does not attempt bidirectional synchronization between two daemon
implementations. Changes made after deliberately rolling back to an older daemon should be treated
as a separate recovery event; repeatedly importing the legacy table would incorrectly resurrect
entries that a user intentionally removed from the current `denylist`.

Inspect the internal audit row without changing it:

```sh
magisk --sqlite 'SELECT * FROM hide_migration_v13'
```

## Existing-database recovery

Before changing an existing schema, the daemon uses SQLite's online backup API to create
`/data/adb/magisk.db.v12.bak`. It verifies that the backup is a root-owned mode-`0600` regular file,
has `user_version=12`, passes `PRAGMA quick_check`, and is durably synced. Atomic no-replace
publication prevents a concurrent or interrupted attempt from overwriting the first valid backup.

The marker creation, audit counts, and union run under one `BEGIN IMMEDIATE` transaction. Any real
error rolls the transaction back and database initialization fails closed instead of deleting the
database. Commit-boundary tests accept only the original unmarked v12 state or the complete marked
v12 state.

## Test coverage

Host tests execute the exact native SQL against empty, legacy-only, canonical-only, divergent,
malformed, and SuList fixtures. They also cover read-only/full databases, forced schema failures,
abrupt process death, and repeat execution.

```sh
python3 -m unittest tests.system_mode.test_hide_migration \
  tests.security_lab.test_sqlite_faults -v
```
