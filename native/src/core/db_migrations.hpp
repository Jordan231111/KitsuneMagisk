#pragma once

// Version 13 makes `denylist` the canonical normal-hide table. The legacy `hidelist`
// is deliberately retained as an in-database rollback/audit source. The conflict
// policy is a security-conservative union: valid legacy rows are added without
// deleting or replacing any existing denylist rows. SuList remains independent.
//
// Keep this SQL in a standalone raw string: repository host tests execute the exact
// migration text against the fixture matrix without requiring an Android runtime.
inline constexpr char HIDE_TABLE_MIGRATION_V13[] = R"sql(
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS settings (
    key TEXT,
    value INT,
    PRIMARY KEY(key)
);
CREATE TABLE IF NOT EXISTS hidelist (
    package_name TEXT,
    process TEXT,
    PRIMARY KEY(package_name, process)
);
CREATE TABLE IF NOT EXISTS denylist (
    package_name TEXT,
    process TEXT,
    PRIMARY KEY(package_name, process)
);
CREATE TABLE IF NOT EXISTS sulist (
    package_name TEXT,
    process TEXT,
    PRIMARY KEY(package_name, process)
);
CREATE TABLE IF NOT EXISTS hide_migration_v13 (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    strategy TEXT NOT NULL,
    source_hidelist_rows INTEGER NOT NULL,
    source_denylist_rows INTEGER NOT NULL,
    source_sulist_rows INTEGER NOT NULL,
    overlap_rows INTEGER NOT NULL,
    migrated_rows INTEGER NOT NULL,
    malformed_hidelist_rows INTEGER NOT NULL,
    sulist_enabled INTEGER NOT NULL
);
DELETE FROM hide_migration_v13 WHERE id = 1;
INSERT INTO hide_migration_v13 (
    id,
    strategy,
    source_hidelist_rows,
    source_denylist_rows,
    source_sulist_rows,
    overlap_rows,
    migrated_rows,
    malformed_hidelist_rows,
    sulist_enabled
)
SELECT
    1,
    'union-preserve-legacy',
    (SELECT COUNT(*) FROM hidelist),
    (SELECT COUNT(*) FROM denylist),
    (SELECT COUNT(*) FROM sulist),
    (
        SELECT COUNT(*)
        FROM hidelist AS legacy
        INNER JOIN denylist AS canonical
            ON canonical.package_name = legacy.package_name
            AND canonical.process = legacy.process
        WHERE legacy.package_name IS NOT NULL
            AND legacy.process IS NOT NULL
            AND legacy.package_name <> ''
            AND legacy.process <> ''
    ),
    0,
    (
        SELECT COUNT(*)
        FROM hidelist
        WHERE package_name IS NULL
            OR process IS NULL
            OR package_name = ''
            OR process = ''
    ),
    COALESCE((SELECT value FROM settings WHERE key = 'sulist' LIMIT 1), 0);
INSERT OR IGNORE INTO denylist (package_name, process)
SELECT package_name, process
FROM hidelist
WHERE package_name IS NOT NULL
    AND process IS NOT NULL
    AND package_name <> ''
    AND process <> '';
UPDATE hide_migration_v13
SET migrated_rows = changes()
WHERE id = 1;
PRAGMA user_version = 13;
COMMIT;
)sql";
