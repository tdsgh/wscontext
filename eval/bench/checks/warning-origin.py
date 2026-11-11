"""warning-origin: WARNING_ORIGIN.txt names the commit that added the over-budget warning and its branch."""

import os
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))
COMMIT = "d73eae9e527a782be5f57b783c9767c474d92ae1"  # "Warn when a category passes 90 percent of its budget"
BRANCH = "feature-budgets"


class WarningOrigin(unittest.TestCase):
    def test_answer(self):
        with open(os.path.join(REPO, "WARNING_ORIGIN.txt"), encoding="utf-8") as f:
            lines = [line.strip() for line in f.read().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, lines)
        sha, branch = lines
        self.assertTrue(len(sha) >= 7 and COMMIT.startswith(sha.lower()), f"commit {sha!r}")
        self.assertEqual(branch, BRANCH)


if __name__ == "__main__":
    unittest.main()
