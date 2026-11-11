"""sqlite-timings: SQLITE_TIMINGS.txt holds the JSON and SQLite timings measured when SQLite was evaluated."""

import os
import unittest

REPO = os.path.dirname(os.path.abspath(__file__))
MEASURED = (44.55, 3.62)  # milliseconds: JSON, SQLite (DECISIONS.md, "Rejected: SQLite backend")


class SqliteTimings(unittest.TestCase):
    def test_answer(self):
        with open(os.path.join(REPO, "SQLITE_TIMINGS.txt"), encoding="utf-8") as f:
            lines = [line.strip() for line in f.read().splitlines() if line.strip()]
        self.assertEqual(len(lines), 2, lines)
        found = tuple(float(line.removesuffix("ms").strip()) for line in lines)
        self.assertEqual(found, MEASURED)


if __name__ == "__main__":
    unittest.main()
