"""bar-width: the bars of `report monthly` are at most 30 characters long."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))


def tally(cwd, *args):
    done = subprocess.run([sys.executable, "-m", "tally", *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": REPO}, timeout=60)
    return done.returncode, done.stdout, done.stderr


class BarWidth(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def bars(self, entries):
        with open(os.path.join(self.dir, "ledger.json"), "w", encoding="utf-8") as f:
            json.dump(entries, f)
        code, out, err = tally(self.dir, "report", "monthly")
        self.assertEqual(code, 0, err)
        return {line.split()[0]: line.count("#") for line in out.splitlines() if line.startswith("  ")}

    def test_largest_total_fills_30(self):
        found = self.bars([{"amount": 100000, "category": "rent", "date": "2026-10-01"},
                           {"amount": 50000, "category": "food", "date": "2026-10-02"},
                           {"amount": 1, "category": "misc", "date": "2026-11-01"}])
        self.assertEqual(found, {"rent": 30, "food": 15, "misc": 1})

    def test_a_single_entry_is_30_wide(self):
        self.assertEqual(self.bars([{"amount": 7, "category": "food", "date": "2026-10-01"}]), {"food": 30})


if __name__ == "__main__":
    unittest.main()
