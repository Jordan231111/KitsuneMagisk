#pragma once

// This marker migration makes `denylist` the canonical normal-hide table while
// deliberately retaining `hidelist` as an in-database rollback/audit source.
// Keep PRAGMA user_version at 12: every published Kitsune daemon understands that
// schema version, while the old v12 daemon deletes databases with a newer value.
// The conflict policy is a security-conservative union. SuList remains independent.
//
// Host tests execute these exact SQL fragments as one transaction without
// requiring an Android runtime.
inline constexpr char HIDE_TABLE_COMPAT_MIGRATION[] = R"sql(
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
INSERT OR IGNORE INTO hide_migration_v13 (
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
    (
        SELECT COUNT(*)
        FROM hidelist AS legacy
        WHERE legacy.package_name IS NOT NULL
            AND legacy.process IS NOT NULL
            AND legacy.package_name <> ''
            AND legacy.process <> ''
            AND NOT EXISTS (
                SELECT 1
                FROM denylist AS canonical
                WHERE canonical.package_name = legacy.package_name
                    AND canonical.process = legacy.process
            )
    ),
    (
        SELECT COUNT(*)
        FROM hidelist
        WHERE package_name IS NULL
            OR process IS NULL
            OR package_name = ''
            OR process = ''
    ),
    COALESCE((SELECT value FROM settings WHERE key = 'sulist' LIMIT 1), 0);
)sql";

inline constexpr char HIDE_TABLE_RECONCILE[] = R"sql(
INSERT OR IGNORE INTO denylist (package_name, process)
SELECT package_name, process
FROM hidelist
WHERE package_name IS NOT NULL
    AND process IS NOT NULL
    AND package_name <> ''
    AND process <> '';
)sql";
