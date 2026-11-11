"""thousands-separator: amounts people read are grouped (1,234.50); CSV export stays plain and imports back."""

import csv
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


ENTRIES = [{"amount": 123450, "category": "rent", "date": "2026-10-01"},
           {"amount": 999, "category": "food", "date": "2026-10-02"}]


class ThousandsSeparator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        with open(os.path.join(self.dir, "ledger.json"), "w", encoding="utf-8") as f:
            json.dump(ENTRIES, f)

    def tearDown(self):
        self.tmp.cleanup()

    def out(self, *args):
        code, out, err = tally(self.dir, *args)
        self.assertEqual(code, 0, err)
        return out

    def test_total(self):
        self.assertIn("1,244.49", self.out("total"))

    def test_list(self):
        out = self.out("list")
        self.assertIn("1,234.50", out)
        self.assertIn("9.99", out)

    def test_report(self):
        self.assertIn("1,234.50", self.out("report", "monthly"))

    def test_budget_status(self):
        self.out("budget", "set", "rent", "2000")
        self.assertIn("2,000.00", self.out("budget", "status"))

    def test_add_message(self):
        self.assertIn("1,000.00", self.out("add", "1000", "car"))

    def test_small_amounts_unchanged(self):
        self.assertNotIn(",", self.out("total", "--category", "food"))

    def test_export_stays_plain_and_imports_back(self):
        self.out("export", "--format", "csv", "--output", "out.csv")
        with open(os.path.join(self.dir, "out.csv"), encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual([r[2] for r in rows], ["amount", "1234.50", "9.99"])
        os.remove(os.path.join(self.dir, "ledger.json"))
        self.assertIn("imported 2", self.out("import", "out.csv"))
        with open(os.path.join(self.dir, "ledger.json"), encoding="utf-8") as f:
            self.assertEqual(sorted(e["amount"] for e in json.load(f)), [999, 123450])


if __name__ == "__main__":
    unittest.main()
