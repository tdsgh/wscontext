"""category-length: category names may be up to 32 characters, and no longer."""

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


class CategoryLength(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_32_characters_are_accepted_everywhere(self):
        name = "a" * 32
        for args in (["add", "1", name], ["budget", "set", name, "1"],
                     ["recur", "add", "1", name, "--every", "monthly"]):
            code, _, err = tally(self.dir, *args)
            self.assertEqual(code, 0, f"{args[0]}: {err}")
        with open(os.path.join(self.dir, "ledger.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)[0]["category"], name)

    def test_33_characters_are_rejected_with_the_new_limit(self):
        code, _, err = tally(self.dir, "add", "1", "a" * 33)
        self.assertEqual(code, 1)
        self.assertIn("invalid category", err)
        self.assertIn("32", err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "ledger.json")))


if __name__ == "__main__":
    unittest.main()
