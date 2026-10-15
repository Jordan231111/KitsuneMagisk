"""Keep the manager's bounded diagnostic vocabulary aligned with backend schemas."""
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


class ManagerContractTest(unittest.TestCase):
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
