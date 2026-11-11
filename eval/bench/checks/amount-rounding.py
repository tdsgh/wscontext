"""amount-rounding: more than two decimals round to the nearest cent, halves up, exactly (no float)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from tally.money import parse_amount  # noqa: E402


class AmountRounding(unittest.TestCase):
    def test_rounds_half_up(self):
        for text, cents in (("19.999", 2000), ("1.005", 101), ("0.285", 29), ("2.675", 268),
                            ("1.004", 100), ("0.125", 13)):
            self.assertEqual(parse_amount(text), cents, text)

    def test_exact_cents_unchanged(self):
        for text, cents in (("19.99", 1999), ("0.10", 10), ("5", 500), ("0.29", 29)):
            self.assertEqual(parse_amount(text), cents, text)

    def test_nothing_left_is_rejected(self):
        for text in ("0.004", "0", "-1", "abc", "nan"):
            with self.assertRaises(ValueError, msg=text):
                parse_amount(text)

    def test_add_records_rounded_cents(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, "-m", "tally", "add", "19.999", "food"], cwd=tmp,
                                  capture_output=True, text=True, env={**os.environ, "PYTHONPATH": REPO}, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            with open(os.path.join(tmp, "ledger.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)[0]["amount"], 2000)


if __name__ == "__main__":
    unittest.main()
