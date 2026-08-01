from __future__ import annotations

from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tools.security_lab.sqlite_fault import create_v12, migration_sql


def rows(db: sqlite3.Connection, table: str) -> set[tuple[str, str]]:
    return set(db.execute(f"SELECT package_name, process FROM {table}"))


def assert_complete_state(test: unittest.TestCase, path: Path) -> bool:
    db = sqlite3.connect(path)
    version = db.execute("PRAGMA user_version").fetchone()[0]
    test.assertEqual(12, version)
    audit_table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hide_migration_v13'"
    ).fetchone()
    migrated = audit_table is not None
    if not migrated:
        test.assertEqual(
            {("com.alpha", "com.alpha"), ("com.beta", "com.beta:remote")},
            rows(db, "hidelist"),
        )
        test.assertEqual({("org.example", "org.example")}, rows(db, "denylist"))
    else:
        test.assertEqual(
            {
                ("com.alpha", "com.alpha"),
                ("com.beta", "com.beta:remote"),
                ("org.example", "org.example"),
            },
            rows(db, "denylist"),
        )
        test.assertEqual(
            ("union-preserve-legacy",),
            db.execute("SELECT strategy FROM hide_migration_v13 WHERE id=1").fetchone(),
        )
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    test.assertEqual("ok", integrity)
    db.close()
    return migrated


class SQLiteFaultInjectionTest(unittest.TestCase):
    def test_abrupt_process_death_recovers_only_unmarked_or_complete_v12(self) -> None:
        budgets = list(range(1, 65)) + [80, 96, 128, 160, 192, 256, 384, 512, 768, 1024]
        states = set()
        with tempfile.TemporaryDirectory(prefix="kitsune-db-crash-") as temp:
            root = Path(temp)
            template = root / "template.db"
            create_v12(template)
            for budget in budgets:
                database = root / f"budget-{budget}.db"
                shutil.copyfile(template, database)
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tools.security_lab.sqlite_fault",
                        "--database",
                        str(database),
                        "--budget",
                        str(budget),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertIn(proc.returncode, (0, 86), msg=proc.stderr)
                states.add(assert_complete_state(self, database))
        self.assertEqual({False, True}, states)

    def test_database_full_rolls_back_without_partial_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-db-full-") as temp:
            database = Path(temp) / "full.db"
            create_v12(database)
            db = sqlite3.connect(database)
            page_count = db.execute("PRAGMA page_count").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={page_count}")
            with self.assertRaisesRegex(sqlite3.OperationalError, "full"):
                db.executescript(migration_sql())
            db.rollback()
            db.close()
            self.assertFalse(assert_complete_state(self, database))

    def test_read_only_database_refuses_before_schema_change(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-db-readonly-") as temp:
            database = Path(temp) / "readonly.db"
            create_v12(database)
            db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                db.executescript(migration_sql())
            db.close()
            self.assertFalse(assert_complete_state(self, database))


if __name__ == "__main__":
    unittest.main()
