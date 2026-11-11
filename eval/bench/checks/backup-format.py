"""backup-format: `backup --output FILE` and `restore FILE`, in a readable format that is safe to load, not pickle."""

import glob
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


DATA = {"ledger.json": [{"amount": 1999, "category": "food", "time": "2026-11-01T10:00:00Z", "tags": ["trip"]}],
        "budgets.json": {"food": 30000},
        "recurring.json": [{"amount": 999, "category": "news", "every": "monthly", "start": "2026-11-01",
                            "last": None}]}


class BackupFormat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        for name, value in DATA.items():
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                json.dump(value, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_backup_is_readable_json_and_restores(self):
        code, _, err = tally(self.dir, "backup", "--output", "all.bak")
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.dir, "all.bak"), "rb") as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b"\x80"), "the backup is a pickle")
        json.loads(raw.decode("utf-8"))  # plain text a person can read and diff
        for name in DATA:
            os.remove(os.path.join(self.dir, name))
        code, _, err = tally(self.dir, "restore", "all.bak")
        self.assertEqual(code, 0, err)
        for name, value in DATA.items():
            with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                self.assertEqual(json.load(f), value, name)

    def test_no_pickle_in_the_code(self):
        for path in glob.glob(os.path.join(REPO, "tally", "*.py")):
            with open(path, encoding="utf-8") as f:
                self.assertNotIn("pickle", f.read(), path)


if __name__ == "__main__":
    unittest.main()
