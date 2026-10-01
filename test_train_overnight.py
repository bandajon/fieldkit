import sys
import types
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import selfloop


class Reached(Exception):
    pass


def at(hour):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 1, hour, 0, tzinfo=ZoneInfo("Africa/Lusaka"))
    return FakeDT


def boom(*a, **k):
    raise Reached


class TrainOvernight(unittest.TestCase):
    def run_at(self, hour, left=6):
        with patch.object(selfloop, "classify_behind", return_value=True), \
             patch.object(selfloop, "datetime", at(hour)), \
             patch.object(selfloop, "load_state", return_value={"classify_backlog": {"left": left}}), \
             patch.object(selfloop, "Lock", boom), \
             patch.dict(sys.modules, {"train": types.SimpleNamespace()}):
            return selfloop.train_pass()

    def test_night_small_backlog_skips_yield(self):
        with self.assertRaises(Reached):
            self.run_at(1, 6)

    def test_night_big_backlog_yields(self):
        self.assertIsNone(self.run_at(1, 500))

    def test_day_yields(self):
        self.assertIsNone(self.run_at(14, 6))

    def test_night_edges(self):
        with self.assertRaises(Reached):
            self.run_at(4)
        self.assertIsNone(self.run_at(5))


if __name__ == "__main__":
    unittest.main()
