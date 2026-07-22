# Hide-table migration v13

Database version 13 completes the storage change started by `25fa2159`. Normal MagiskHide mode uses
`denylist` as its canonical table; SuList continues to use the independent `sulist` table.

## Conflict rule

The migration uses a security-conservative union:

1. Preserve every existing `denylist` row.
2. Add each valid `hidelist` row that is not already in `denylist`.
3. Leave `hidelist` intact as a legacy audit/rollback source.
4. Do not copy null or empty package/process values into the active table; retain them in
   `hidelist` and count them as malformed.
5. Never copy, clear, or reinterpret `sulist` rows.

This can hide more processes when the two legacy tables diverge, which is safer than silently
dropping an existing hide selection but can affect app behavior. The exact source, overlap,
migrated, malformed, and SuList counts are stored in the singleton `hide_migration_v13` row with
strategy `union-preserve-legacy`.

Inspect the audit without modifying it:

```sh
magisk --sqlite 'SELECT * FROM hide_migration_v13'
```

## Transaction and recovery behavior

Before running the SQL transaction, the daemon uses SQLite's online backup API to create and verify
`/data/adb/magisk.db.v12.bak`. The first valid version-12 backup is never overwritten. Migration is
blocked unless the backup is a root-owned, owner-only mode-`0600` regular file with one link,
reports `user_version=12`, passes `PRAGMA quick_check`, and is fsynced with its parent directory.
Publication uses an atomic no-replace hard link, so a concurrently created first backup cannot be
overwritten; an interrupted two-link publication is recognized and completed on the next run.

The exact SQL in `native/src/core/db_migrations.hpp` performs all table creation, union, audit
recording, and `PRAGMA user_version=13` under `BEGIN IMMEDIATE`. Any error triggers an explicit
rollback. Database initialization now fails closed; it no longer unlinks the complete Magisk
database after an arbitrary schema or migration error.

SQLite may surface cancellation at the commit boundary after the transaction is already durable.
The daemon rolls back first, then accepts that late-error state only when autocommit is restored and
both `user_version=13` and the singleton migration audit row are visible. Interruption tests require
every boundary to expose either the complete original v12 state or the complete audited v13 state;
a partial mixture is never accepted.

An older version-12 daemon does not understand version 13. Do not treat an APK/native rollback as a
database rollback: restore the verified `.v12.bak` from a vendor-root/recovery context before
starting the older daemon. The retained `hidelist` table is useful for forward repair but does not
replace the full database backup because SU policies and settings share the same database.

## Fixture coverage

Repository tests execute the exact native SQL against:

- an empty version-12 database;
- `hidelist` only;
- `denylist` only;
- overlapping and divergent tables;
- SuList enabled with independent rows;
- malformed legacy rows;
- a forced schema failure that must leave `user_version=12` and the original rows intact.

Run them with:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
```
