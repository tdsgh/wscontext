"""currency-prefix: add, budget set and recur add accept a leading dollar sign; CSV import does not."""

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


class CurrencyPrefix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return json.load(f)

    def test_add_takes_a_dollar_sign(self):
        code, _, err = tally(self.dir, "add", "$12.50", "food")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.load("ledger.json")[0]["amount"], 1250)

    def test_budget_set_takes_a_dollar_sign(self):
        code, _, err = tally(self.dir, "budget", "set", "food", "$300")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.load("budgets.json"), {"food": 30000})

    def test_recur_add_takes_a_dollar_sign(self):
        code, _, err = tally(self.dir, "recur", "add", "$9.99", "streaming", "--every", "monthly")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.load("recurring.json")[0]["amount"], 999)

    def test_plain_amounts_still_work(self):
        code, _, err = tally(self.dir, "add", "12.50", "food")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.load("ledger.json")[0]["amount"], 1250)

    def test_import_rejects_a_dollar_sign(self):
        with open(os.path.join(self.dir, "in.csv"), "w", encoding="utf-8") as f:
            f.write("date,category,amount\n2026-11-01,food,$5.00\n")
        code, _, _ = tally(self.dir, "import", "in.csv")
        self.assertNotEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "ledger.json")))

    def test_import_takes_plain_amounts(self):
        with open(os.path.join(self.dir, "in.csv"), "w", encoding="utf-8") as f:
            f.write("date,category,amount\n2026-11-01,food,5.00\n")
        code, _, err = tally(self.dir, "import", "in.csv")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.load("ledger.json")[0]["amount"], 500)


if __name__ == "__main__":
    unittest.main()
