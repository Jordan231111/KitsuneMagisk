from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DatabaseVersionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="kitsune-native-sqlite-")
        cls.binary = Path(cls.temporary.name) / "database"
        source = (ROOT / "native/src/core/sqlite.cpp").read_text()
        native = source[source.index("sqlite3 *open_and_init_db()"):source.index("// Exported from Rust")]
        support = r'''
#include <sqlite3.h>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <string>
#include <string_view>
using namespace std;
struct DbValues {
    sqlite3_stmt *statement;
    int get_int(int column) const { return sqlite3_column_int(statement, column); }
};
using Callback = void (*)(void *, int, const DbValues &);
int sql_exec_impl(sqlite3 *db, string_view sql, void * = nullptr, void * = nullptr,
                  Callback callback = nullptr, void *cookie = nullptr) {
    if (const char *fail = getenv("FAIL_SQL"); fail && sql == fail) return SQLITE_IOERR;
    string text(sql);
    const char *cursor = text.c_str();
    while (*cursor) {
        sqlite3_stmt *statement = nullptr;
        int rc = sqlite3_prepare_v2(db, cursor, -1, &statement, &cursor);
        if (rc != SQLITE_OK) return rc;
        if (!statement) continue;
        while ((rc = sqlite3_step(statement)) == SQLITE_ROW) {
            if (callback) callback(cookie, 0, DbValues{statement});
        }
        sqlite3_finalize(statement);
        if (rc != SQLITE_DONE) return rc;
    }
    return SQLITE_OK;
}
static auto close_database = &sqlite3_close;
#define sqlite3_close close_database
#define DB_VERSION 12
#define DB_VERSION_STR "12"
const char *db_path;
#define MAGISKDB db_path
#define LOGE(...) fprintf(stderr, __VA_ARGS__)
#define sql_chk_log(fn, ...) if (fn(__VA_ARGS__) != SQLITE_OK) return nullptr;
bool load_sqlite() { return true; }
'''
        program = support + native + r'''
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    db_path = argv[1];
    sqlite3 *db = open_and_init_db();
    if (!db) return 1;
    sqlite3_close(db);
    return 0;
}
'''
        path = cls.binary.with_suffix(".cpp")
        path.write_text(program)
        subprocess.run(["c++", "-std=c++20", str(path), "-lsqlite3", "-o", str(cls.binary)],
                       check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def database(self, path: Path, version=13):
        with sqlite3.connect(path) as db:
            db.executescript("""
CREATE TABLE policies(uid INT PRIMARY KEY, policy INT, until INT, logging INT, notification INT);
CREATE TABLE settings(key TEXT PRIMARY KEY, value INT);
CREATE TABLE strings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE denylist(package_name TEXT, process TEXT, PRIMARY KEY(package_name,process));
CREATE TABLE hidelist(package_name TEXT, process TEXT, PRIMARY KEY(package_name,process));
CREATE TABLE sulist(package_name TEXT, process TEXT, PRIMARY KEY(package_name,process));
CREATE TABLE hide_migration_v13(id INTEGER PRIMARY KEY, strategy TEXT,
 source_hidelist_rows INT, source_denylist_rows INT, source_sulist_rows INT,
 overlap_rows INT, migrated_rows INT, malformed_hidelist_rows INT, sulist_enabled INT);
INSERT INTO policies VALUES(2000,2,0,0,0),(10001,1,0,1,1);
INSERT INTO settings VALUES('sulist',1),('denylist',0),('zygisk',0);
INSERT INTO strings VALUES('retained','user setting');
INSERT INTO denylist VALUES('one','one'),('two','two');
INSERT INTO hidelist SELECT * FROM denylist;
INSERT INTO sulist VALUES('allowed','allowed');
INSERT INTO hide_migration_v13 VALUES(1,'union-preserve-legacy',2,1,1,1,1,0,1);
""")
            db.execute(f"PRAGMA user_version={version}")

    def dump(self, path):
        with sqlite3.connect(path) as db:
            self.assertEqual("ok", db.execute("PRAGMA integrity_check").fetchone()[0])
            return list(db.iterdump()), db.execute("PRAGMA user_version").fetchone()[0]

    def run_native(self, path, failure=None):
        env = dict(os.environ)
        if failure:
            env["FAIL_SQL"] = failure
        return subprocess.run([str(self.binary), str(path)], env=env,
                              capture_output=True, text=True, timeout=10)

    def test_newer_schemas_are_preserved_without_conversion(self):
        cases = [
            (13, None),
            (14, None),
            (13, "DROP TABLE hide_migration_v13"),
            (13, "UPDATE hide_migration_v13 SET migrated_rows=99"),
            (13, "UPDATE hide_migration_v13 SET strategy='unknown'"),
            (13, "ALTER TABLE policies RENAME COLUMN until TO future_expiry"),
        ]
        for version, mutation in cases:
            with self.subTest(version=version, mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "magisk.db"
                self.database(path, version)
                if mutation:
                    with sqlite3.connect(path) as db:
                        db.execute(mutation)
                before = path.read_bytes()
                self.assertNotEqual(0, self.run_native(path).returncode)
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(version, self.dump(path)[1])

    def test_v12_keeps_legacy_tables_and_policies(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "magisk.db"
            self.database(path, 12)
            before = path.read_bytes()
            self.assertEqual(0, self.run_native(path).returncode)
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
