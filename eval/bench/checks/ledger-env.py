"""ledger-env: TALLY_LEDGER names the ledger file for every command that reads or writes entries."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))


def tally(cwd, *args, ledger=None):
    env = {k: v for k, v in os.environ.items() if k != "TALLY_LEDGER"}
    if ledger:
        env["TALLY_LEDGER"] = ledger
    done = subprocess.run([sys.executable, "-m", "tally", *args], cwd=cwd, capture_output=True, text=True,
                          env={**env, "PYTHONPATH": REPO}, timeout=60)
    return done.returncode, done.stdout, done.stderr


class LedgerEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.ledger = os.path.join(self.dir, "books", "mine.json")
        os.mkdir(os.path.dirname(self.ledger))

    def tearDown(self):
        self.tmp.cleanup()

    def ok(self, *args):
        code, out, err = tally(self.dir, *args, ledger=self.ledger)
        self.assertEqual(code, 0, f"{args}: {err}")
        return out

    def entries(self):
        with open(self.ledger, encoding="utf-8") as f:
            return json.load(f)

    def test_every_command_uses_the_named_file(self):
        self.ok("add", "12.50", "food", "--tag", "trip")
        self.ok("add", "3", "car")
        self.assertEqual(len(self.entries()), 2)
        self.assertIn("food", self.ok("list"))
        self.assertIn("15.50", self.ok("total"))
        self.assertIn("food", self.ok("report", "monthly"))
        self.ok("budget", "set", "food", "20")
        self.assertIn("62%", self.ok("budget", "status"))
        code, _, err = tally(self.dir, "add", "6", "food", ledger=self.ledger)
        self.assertEqual(code, 0, err)
        self.assertIn("warning", err)
        self.ok("undo")
        self.assertEqual(len(self.entries()), 2)
        self.ok("export", "--format", "csv", "--output", "out.csv")
        with open(os.path.join(self.dir, "in.csv"), "w", encoding="utf-8") as f:
            f.write("date,category,amount\n2020-01-01,books,4.00\n")
        self.ok("import", "in.csv")
        self.assertEqual(len(self.entries()), 3)
        self.ok("recur", "add", "1", "news", "--every", "monthly")
        self.ok("recur", "apply")
        self.assertEqual(len(self.entries()), 4)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "ledger.json")),
                         "a command wrote ledger.json although TALLY_LEDGER was set")

    def test_unset_keeps_ledger_json(self):
        code, _, err = tally(self.dir, "add", "1", "food")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "ledger.json")))


if __name__ == "__main__":
    unittest.main()
