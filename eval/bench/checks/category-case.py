"""category-case: every command that takes a category treats Food and food as the same category."""

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


class CategoryCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def ok(self, *args):
        code, out, err = tally(self.dir, *args)
        self.assertEqual(code, 0, f"{args}: {err}")
        return out

    def load(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return json.load(f)

    def test_add_stores_lowercase(self):
        self.ok("add", "5", "Food")
        self.assertEqual(self.load("ledger.json")[0]["category"], "food")

    def test_budget_set_stores_lowercase(self):
        self.ok("budget", "set", "FOOD", "10")
        self.assertEqual(self.load("budgets.json"), {"food": 1000})

    def test_recur_add_stores_lowercase(self):
        self.ok("recur", "add", "1", "Streaming", "--every", "monthly")
        self.assertEqual(self.load("recurring.json")[0]["category"], "streaming")

    def test_import_stores_lowercase(self):
        with open(os.path.join(self.dir, "in.csv"), "w", encoding="utf-8") as f:
            f.write("date,category,amount\n2026-11-01,Food,5.00\n")
        self.ok("import", "in.csv")
        self.assertEqual(self.load("ledger.json")[0]["category"], "food")

    def test_total_filter_ignores_case(self):
        self.ok("add", "5", "food")
        self.ok("add", "7", "car")
        self.assertIn("5.00", self.ok("total", "--category", "Food"))
        self.assertIn("5.00", self.ok("total", "--category", "food"))

    def test_other_rules_still_hold(self):
        code, _, err = tally(self.dir, "add", "5", "eating out")
        self.assertEqual(code, 1)
        self.assertIn("invalid category", err)


if __name__ == "__main__":
    unittest.main()
