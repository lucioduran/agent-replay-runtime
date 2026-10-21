import copy
import json
import unittest
from pathlib import Path

from experiments.verify import verify


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.report = json.loads(
            (Path(__file__).resolve().parents[1] / "results/crash-matrix.json").read_text()
        )

    def test_saved_trace_replays_and_matches_sources(self):
        verify(self.report)

    def test_altered_counts_or_source_are_rejected(self):
        changed = copy.deepcopy(self.report)
        changed["summary"]["baseline_effects"] = 1
        with self.assertRaises(AssertionError):
            verify(changed)
        changed = copy.deepcopy(self.report)
        changed["metadata"]["source_sha256"] = {}
        with self.assertRaises(AssertionError):
            verify(changed)

    def test_duplicate_or_missing_cases_are_rejected(self):
        self.report["cases"][0] = copy.deepcopy(self.report["cases"][1])
        with self.assertRaises(AssertionError):
            verify(self.report)
