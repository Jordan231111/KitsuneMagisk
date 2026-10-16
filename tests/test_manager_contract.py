"""Keep the manager's bounded diagnostic vocabulary aligned with backend schemas."""
import json
from pathlib import Path
import re
import subprocess
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class ManagerContractTest(unittest.TestCase):
    def test_installer_only_sets_supported_app_op_after_success(self):
        script = r'''
. "$1"
cp() { :; }
chmod() { :; }
rm() { :; }
su() { return "$INSTALL_RESULT"; }
pm() { return "$INSTALL_RESULT"; }
appops() { printf 'appops:%s\n' "$*"; }
SDK_INT="$2"
INSTALL_RESULT="$3"
adb_pm_install fixture.apk fixture.package
'''
        for sdk in (23, 24, 25, 26, 35):
            for status in (0, 1):
                with self.subTest(sdk=sdk, status=status):
                    result = subprocess.run(['bash', '-c', script, 'test',
                                             str(ROOT / 'scripts/app_functions.sh'), str(sdk), str(status)],
                                            text=True, capture_output=True, timeout=5)
                    self.assertEqual(status, result.returncode, result.stderr)
                    expected = 'appops:set fixture.package REQUEST_INSTALL_PACKAGES allow\n' if sdk >= 26 and status == 0 else ''
                    self.assertEqual(expected, result.stdout)

    def test_transaction_and_doctor_vocabularies_match_the_contract(self):
        source = (ROOT / 'app/core/src/main/java/com/topjohnwu/magisk/core/model/ManagerDiagnostics.kt').read_text()
        states = re.search(r'val transactionStates = setOf\((.*?)\)', source, re.S).group(1)
        actual = set(re.findall(r'"([A-Z_]+)"', states))
        contract = json.loads((ROOT / 'tools/system_mode/contracts/install-state-machine.json').read_text())
        self.assertEqual(set(contract['states']), actual)
        verdicts = re.search(r'require\(verdict in setOf\((.*?)\)', source, re.S).group(1)
        schema = json.loads((ROOT / 'tools/system_mode/schemas/doctor-v1.schema.json').read_text())
        self.assertEqual(set(schema['properties']['assessment']['properties']['verdict']['enum']),
                         set(re.findall(r'"([a-z_]+)"', verdicts)))

    def test_product_locales_retain_format_arguments(self):
        resources = ROOT / 'app/core/src/main/res'
        default = {x.attrib['name']: x.text or '' for x in ET.parse(resources / 'values/strings.xml').getroot() if x.tag == 'string'}
        expected = None
        for locale in ['zh-rCN', 'zh-rTW', 'vi', 'ru', 'tr']:
            strings = {x.attrib['name']: x.text or '' for x in ET.parse(resources / f'values-{locale}/kitsune.xml').getroot()}
            if expected is None:
                expected = set(strings)
            self.assertEqual(expected, set(strings))
            for key, value in strings.items():
                self.assertIn(key, default)
                self.assertTrue(value.strip(), (locale, key))
                self.assertEqual(re.findall(r'%\d+\$[ds]', default[key]), re.findall(r'%\d+\$[ds]', value), (locale, key))
