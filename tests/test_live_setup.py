"""Exercise bootstrap discovery and completion independently of the old SU socket."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LiveSetupTest(unittest.TestCase):
    def prepare(self, root):
        source = (ROOT / "scripts/live_setup.sh").read_text()
        source = source[source.index("cd /data/local/tmp"):source.index("\npm install ")]
        source = source.replace("/data/local/tmp", str(root))
        (root / "busybox").write_text('''#!/bin/sh
case "$1" in
  id) [ "$TEST_PRIVILEGED" = 1 ] && echo 0 || echo 2000 ;;
  setsid) shift; exec "$@" ;;
  *) exec /usr/bin/env PATH="$TEST_BIN:/bin:/usr/bin" "$@" ;;
esac
''')
        (root / "busybox").chmod(0o755)
        (root / "id").write_text('#!/bin/sh\n[ "$TEST_PRIVILEGED" = 1 ] && echo 0 || echo 2000\n')
        (root / "id").chmod(0o755)
        script = root / "live_setup.sh"
        script.write_text(source + '\n/bin/sleep 0.1\nexit "$WORKER_RESULT"\n')
        return script, dict(os.environ, PATH=str(root), FIRST_STAGE="", TEST_PRIVILEGED="", TEST_BIN=str(root))

    def test_path_su_receives_command_and_preserves_failure(self):
        with tempfile.TemporaryDirectory(prefix="kitsune-live-setup-") as directory:
            root = Path(directory)
            script, env = self.prepare(root)
            (root / "su").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 37\n')
            (root / "su").chmod(0o755)
            result = subprocess.run(["/bin/sh", str(script)], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(37, result.returncode)
            self.assertEqual("-c", result.stdout.splitlines()[0])
            self.assertIn("setsid", result.stdout)
            self.assertIn("--root-worker", result.stdout)
            (root / "su").unlink()
            result = subprocess.run(["/bin/sh", str(script)], cwd=root, env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(1, result.returncode)
            self.assertIn("Bootstrap root is required", result.stderr)

    def test_worker_result_survives_bootstrap_disconnect(self):
        for status in (0, 23):
            with self.subTest(status=status), tempfile.TemporaryDirectory(prefix="kitsune-live-setup-") as directory:
                root = Path(directory)
                script, env = self.prepare(root)
                (root / "su").write_text('''#!/bin/sh
export TEST_PRIVILEGED=1
/bin/sh -c "$2"
while :; do
  for marker in .magisk-live.*/started; do
    [ ! -f "$marker" ] || exit 37
  done
  /bin/sleep 0.01
done
''')
                (root / "su").chmod(0o755)
                result = subprocess.run(["/bin/sh", str(script)], cwd=root,
                                        env=dict(env, WORKER_RESULT=str(status)),
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertFalse(list(root.glob(".magisk-live.*")))


if __name__ == "__main__":
    unittest.main()
