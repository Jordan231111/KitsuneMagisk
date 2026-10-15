"""Exercise the production consent parser and socket deadlines on the host."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SuConsentTest(unittest.TestCase):
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
