from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_HEADER = ROOT / "native" / "src" / "core" / "db_migrations.hpp"


def sql_constant(name: str, header: Path = MIGRATION_HEADER) -> str:
    text = header.read_text(encoding="utf-8")
    pattern = rf'inline constexpr char {re.escape(name)}\[\]\s*=\s*R"sql\((.*?)\)sql";'
    matches = re.findall(pattern, text, flags=re.DOTALL)
    if len(matches) != 1:
        raise ValueError(f"expected exactly one {name} raw string")
    return matches[0]


def reconcile_sql(header: Path = MIGRATION_HEADER) -> str:
    return sql_constant("HIDE_TABLE_RECONCILE", header)


def migration_sql(header: Path = MIGRATION_HEADER) -> str:
    return "\n".join(
        (
            sql_constant("HIDE_TABLE_COMPAT_MIGRATION", header),
            reconcile_sql(header),
            "COMMIT;",
        )
    )


def create_v12(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
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
        INSERT INTO settings VALUES('sulist', 0);
        INSERT INTO hidelist VALUES('com.alpha', 'com.alpha');
        INSERT INTO hidelist VALUES('com.beta', 'com.beta:remote');
        INSERT INTO denylist VALUES('org.example', 'org.example');
        PRAGMA user_version=12;
        """
    )
    db.commit()
    db.close()


def run_with_crash_budget(path: Path, budget: int) -> None:
    db = sqlite3.connect(path)
    calls = 0

    def progress() -> int:
        nonlocal calls
        calls += 1
        if calls >= budget:
            os._exit(86)
        return 0

    db.set_progress_handler(progress, 1)
    db.executescript(migration_sql())
    db.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SQLite migration crash-injection helper")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--budget", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.budget <= 0:
        print("budget must be positive", file=sys.stderr)
        return 2
    run_with_crash_budget(args.database, args.budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
