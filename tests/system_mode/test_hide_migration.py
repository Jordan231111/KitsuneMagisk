from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import unittest


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "hide_migration"
MIGRATION_HEADER = ROOT / "native" / "src" / "core" / "db_migrations.hpp"


def migration_sql() -> str:
    text = MIGRATION_HEADER.read_text(encoding="utf-8")
    matches = re.findall(r'R"sql\((.*?)\)sql"', text, flags=re.DOTALL)
    if len(matches) != 1:
        raise AssertionError("expected exactly one v13 migration raw string")
    return matches[0]


def create_fixture(fixture: dict) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE settings (key TEXT, value INT, PRIMARY KEY(key));
        CREATE TABLE hidelist (
            package_name TEXT,
            process TEXT,
            PRIMARY KEY(package_name, process)
        );
        CREATE TABLE denylist (
            package_name TEXT,
            process TEXT,
            PRIMARY KEY(package_name, process)
        );
        CREATE TABLE sulist (
            package_name TEXT,
            process TEXT,
            PRIMARY KEY(package_name, process)
        );
        """
    )
    db.executemany("INSERT INTO settings(key, value) VALUES(?, ?)", fixture["settings"].items())
    for table in ("hidelist", "denylist", "sulist"):
        db.executemany(
            f"INSERT INTO {table}(package_name, process) VALUES(?, ?)",
            fixture[table],
        )
    db.execute(f"PRAGMA user_version = {fixture['user_version']}")
    db.commit()
    return db


def rows(db: sqlite3.Connection, table: str) -> list[list[str | None]]:
    result = db.execute(f"SELECT package_name, process FROM {table}").fetchall()
    return [list(row) for row in sorted(result, key=lambda row: repr(row))]


class HideMigrationTest(unittest.TestCase):
    def test_native_path_backs_up_before_running_migration(self) -> None:
        source = (ROOT / "native" / "src" / "core" / "db.cpp").read_text()
        v12_checkpoint = source.index('sqlite3_exec(db, "PRAGMA user_version=12"')
        backup_call = source.index("err = backup_v12_database(db);")
        migration_call = source.index("sqlite3_exec(db, HIDE_TABLE_MIGRATION_V13")
        self.assertLess(v12_checkpoint, backup_call)
        self.assertLess(backup_call, migration_call)
        self.assertNotIn("unlink(MAGISKDB);", source)
        self.assertIn("link(backup_tmp, backup_path)", source)
        self.assertNotIn("rename(backup_tmp, backup_path)", source)

    def test_fixture_matrix_uses_exact_native_sql(self) -> None:
        paths = sorted(FIXTURES.glob("*.json"))
        self.assertGreaterEqual(len(paths), 5)
        sql = migration_sql()
        for path in paths:
            with self.subTest(path=path.name):
                fixture = json.loads(path.read_text(encoding="utf-8"))
                db = create_fixture(fixture)
                before_hidelist = rows(db, "hidelist")
                db.executescript(sql)

                self.assertEqual(13, db.execute("PRAGMA user_version").fetchone()[0])
                self.assertEqual(
                    sorted(fixture["expected"]["denylist"], key=repr),
                    rows(db, "denylist"),
                )
                self.assertEqual(
                    sorted(fixture["expected"]["sulist"], key=repr),
                    rows(db, "sulist"),
                )
                if fixture["expected"]["hidelist_preserved"]:
                    self.assertEqual(before_hidelist, rows(db, "hidelist"))

                cursor = db.execute("SELECT * FROM hide_migration_v13 WHERE id = 1")
                column_names = [description[0] for description in cursor.description]
                audit = dict(zip(column_names, cursor.fetchone()))
                self.assertEqual("union-preserve-legacy", audit.pop("strategy"))
                self.assertEqual(1, audit.pop("id"))
                self.assertEqual(fixture["expected"]["audit"], audit)
                db.close()

    def test_schema_failure_rolls_back_without_advancing_version(self) -> None:
        db = sqlite3.connect(":memory:")
        db.executescript(
            """
            CREATE TABLE settings (key TEXT, value INT, PRIMARY KEY(key));
            CREATE TABLE hidelist (
                package_name TEXT,
                process TEXT,
                PRIMARY KEY(package_name, process)
            );
            CREATE TABLE denylist (unexpected_column TEXT);
            INSERT INTO hidelist VALUES('com.example.keep', 'com.example.keep');
            PRAGMA user_version = 12;
            """
        )
        with self.assertRaises(sqlite3.OperationalError):
            db.executescript(migration_sql())
        db.rollback()  # Mirrors open_and_init_db's explicit error rollback.

        self.assertEqual(12, db.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(
            [("com.example.keep", "com.example.keep")],
            db.execute("SELECT * FROM hidelist").fetchall(),
        )
        audit_table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hide_migration_v13'"
        ).fetchone()
        self.assertIsNone(audit_table)
        db.close()

    def test_interruptions_leave_only_complete_v12_or_v13_state(self) -> None:
        fixture = {
            "user_version": 12,
            "settings": {"sulist": 0},
            "hidelist": [["com.alpha", "com.alpha"], ["com.beta", "com.beta:remote"]],
            "denylist": [["org.example", "org.example"]],
            "sulist": [],
        }
        interrupted = committed = 0
        for budget in range(1, 451):
            db = create_fixture(fixture)
            before_hidelist = rows(db, "hidelist")
            before_denylist = rows(db, "denylist")
            calls = 0

            def progress() -> int:
                nonlocal calls
                calls += 1
                return int(calls >= budget)

            db.set_progress_handler(progress, 1)
            try:
                db.executescript(migration_sql())
            except sqlite3.OperationalError as exc:
                self.assertIn("interrupted", str(exc).lower())
                db.set_progress_handler(None, 0)
                db.rollback()
                interrupted += 1
            else:
                db.set_progress_handler(None, 0)

            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 12:
                self.assertEqual(before_hidelist, rows(db, "hidelist"))
                self.assertEqual(before_denylist, rows(db, "denylist"))
            else:
                self.assertEqual(13, version)
                self.assertEqual(
                    {
                        ("com.alpha", "com.alpha"),
                        ("com.beta", "com.beta:remote"),
                        ("org.example", "org.example"),
                    },
                    {tuple(row) for row in rows(db, "denylist")},
                )
                audit = db.execute(
                    "SELECT strategy FROM hide_migration_v13 WHERE id=1"
                ).fetchone()
                self.assertEqual(("union-preserve-legacy",), audit)
                committed += 1
            db.close()
        self.assertGreater(interrupted, 0)
        self.assertGreater(committed, 0)


if __name__ == "__main__":
    unittest.main()
