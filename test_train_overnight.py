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
    def run_at(self, hour):
        with patch.object(selfloop, "classify_behind", return_value=True), \
             patch.object(selfloop, "datetime", at(hour)), \
             patch.object(selfloop, "Lock", boom), \
             patch.dict(sys.modules, {"train": types.SimpleNamespace()}):
            return selfloop.train_pass()

    def test_night_skips_yield(self):
        with self.assertRaises(Reached):
            self.run_at(1)

    def test_day_yields(self):
        self.assertIsNone(self.run_at(14))


if __name__ == "__main__":
    unittest.main()
