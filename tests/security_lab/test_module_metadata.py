from __future__ import annotations

from pathlib import Path
import random
import re
import string
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
UTIL_FUNCTIONS = ROOT / "scripts" / "util_functions.sh"
MODULE_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]+$")


def shell_validation(candidates: list[str]) -> list[bool]:
    script = r'''
BOOTMODE=false
. "$1"
while IFS= read -r candidate; do
  if validate_module_id "$candidate"; then
    printf '1\n'
  else
    printf '0\n'
  fi
done
'''
    proc = subprocess.run(
        ["bash", "-c", script, "bash", str(UTIL_FUNCTIONS)],
        input="\n".join(candidates) + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return [line == "1" for line in proc.stdout.splitlines()]


class ModuleMetadataPropertyTest(unittest.TestCase):
    def test_documented_module_id_examples(self) -> None:
        candidates = [
            "a_module",
            "a.module",
            "module-101",
            "A1",
            "",
            "a",
            "1_module",
            "-a-module",
            "a module",
            ".",
            "..",
            "a/../../data",
            "a*",
            "a?",
            "a\tb",
            "a\\b",
            "a$(id)",
            "a;b",
        ]
        self.assertEqual(
            [bool(MODULE_ID.fullmatch(value)) for value in candidates],
            shell_validation(candidates),
        )

    def test_seeded_property_corpus_matches_the_documented_regex(self) -> None:
        randomizer = random.Random(0x4B495453554E45)
        alphabet = string.ascii_letters + string.digits + "._- /\\*?$;:\t"
        candidates = []
        for _ in range(750):
            length = randomizer.randrange(0, 96)
            candidates.append("".join(randomizer.choice(alphabet) for _ in range(length)))
        expected = [bool(MODULE_ID.fullmatch(value)) for value in candidates]
        self.assertEqual(expected, shell_validation(candidates))

    def test_validation_precedes_the_first_module_path_mutation(self) -> None:
        source = UTIL_FUNCTIONS.read_text(encoding="utf-8")
        install = source[source.index("install_module() {") :]
        validation = install.index('validate_module_id "$MODID"')
        assignment = install.index("MODPATH=$MODULEROOT/$MODID")
        removal = install.index('rm -rf -- "$MODPATH"')
        self.assertLess(validation, assignment)
        self.assertLess(assignment, removal)


if __name__ == "__main__":
    unittest.main()
