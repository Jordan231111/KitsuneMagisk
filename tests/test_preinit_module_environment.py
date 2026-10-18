"""Run the real pre-init budget parser after module installers change IFS."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PreinitModuleEnvironmentTest(unittest.TestCase):
    def run_budget(self, size, separator):
        source = (ROOT / "scripts/util_functions.sh").read_text()
        start = source.index("copy_preinit_files() (")
        header = source[start:source.index("  local PREINITDIR=", start)]
        start = source.index("  local sizes totals", start)
        body = source[start:source.index("  space=$(df ", start)]
        with tempfile.TemporaryDirectory(prefix="kitsune-module-env-") as directory:
            root = Path(directory)
            policy = root / "modules/demo/sepolicy.rule"
            policy.parent.mkdir(parents=True)
            with policy.open("wb") as stream:
                stream.truncate(size)
            program = (
                'ui_print() { printf "%s\\n" "$*"; }\n'
                'stat() { ' + shlex.quote(sys.executable) +
                ' -c \'import os,sys; print(os.stat(sys.argv[1]).st_size)\' "$3"; }\n'
                'EARLY=false\n' + header + body +
                '  printf "accepted:%s:%s\\n" "$bytes" "$entries"\n)\n'
                'IFS="$MODULE_IFS"\ncopy_preinit_files\n'
            ).replace("/data/adb", str(root))
            return subprocess.run([shutil.which("bash") or "/bin/bash", "-c", program], capture_output=True, text=True,
                                  env=dict(os.environ, MODULE_IFS=separator), timeout=5)

    def test_normal_policy_after_newline_only_module_installer(self):
        for separator in (" \t\n", "\n"):
            with self.subTest(separator=repr(separator)):
                result = self.run_budget(790, separator)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("accepted:790:1", result.stdout)

    def test_sparse_oversize_policy_still_rejected(self):
        result = self.run_budget(33554433, "\n")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("exceeds the snapshot budget", result.stdout)


if __name__ == "__main__":
    unittest.main()
