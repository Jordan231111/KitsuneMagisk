"""Exercise the production consent parser and socket deadlines on the host."""
from pathlib import Path
import os
import re
import sqlite3
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SuConsentTest(unittest.TestCase):
    def test_persistence_preserves_flags_and_failed_writes(self):
        source = (ROOT / "native/src/core/su/connect.rs").read_text()
        match = re.search(r'"(INSERT OR REPLACE INTO policies.*?)",', source, re.S)
        self.assertIsNotNone(match)
        query = match[1].replace("\\\n", "")
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE policies(uid INTEGER PRIMARY KEY,policy INTEGER,until INTEGER,logging INTEGER,notification INTEGER)")
            db.execute(query, (17, 2, 0, 0, 17, 17))
            self.assertEqual((17, 2, 0, 1, 1), db.execute("SELECT * FROM policies").fetchone())
            db.execute("UPDATE policies SET logging=0,notification=0")
            db.execute(query, (17, 1, 600, 600, 17, 17))
            saved = db.execute("SELECT * FROM policies").fetchone()
            self.assertEqual((0, 0), saved[-2:])
            self.assertEqual(1, saved[1])
            db.execute("CREATE TRIGGER reject_consent BEFORE INSERT ON policies BEGIN SELECT RAISE(ABORT, 'blocked'); END")
            with self.assertRaises(sqlite3.DatabaseError):
                db.execute(query, (17, 2, 0, 0, 17, 17))
            self.assertEqual(saved, db.execute("SELECT * FROM policies").fetchone())

    @unittest.skipUnless((Path(os.environ.get("ANDROID_HOME", "")) / "ndk/magisk/ONDK_VERSION").is_file(),
                         "Requires configured ONDK; also runs in the artifact build job")
    def test_consent_protocol(self):
        # The module has no Android dependencies; compile its exact production code.
        with tempfile.TemporaryDirectory(prefix="kitsune-su-consent-") as directory:
            source = Path(directory) / "consent.rs"
            source.write_text("mod consent {\n" +
                              (ROOT / "native/src/core/su/consent.rs").read_text() + "\n}")
            binary = Path(directory) / "consent"
            subprocess.run([str(ROOT / "scripts/env.py"), "rustc", "--edition=2024",
                            "--test", str(source), "-o", str(binary)],
                           check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True, timeout=10)
