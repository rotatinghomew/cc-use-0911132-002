import json
import unittest
from pathlib import Path


class AnalysisInputsTests(unittest.TestCase):
    def load_data(self):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json").read_text())

    def test_unequal_observation(self):
        data = self.load_data()
        self.assertTrue(len({row['observed_at'] for row in data['visits']}) > 6)

    def test_registry_exits(self):
        data = self.load_data()
        self.assertTrue(len(data['exits']) == 4)

    def test_rule_versions(self):
        data = self.load_data()
        self.assertTrue({row['rule_version'] for row in data['visits']} == {'old', 'new'})

    def test_source_versions_and_cutoff(self):
        data = self.load_data()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["source_versions"])
        self.assertIn("+00:00", data["as_of"])
