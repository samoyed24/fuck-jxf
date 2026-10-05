"""Throwaway verification: active_trip logic + full trip-skip branch offline."""
import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

with patch.dict(sys.modules, {"ddddocr": Mock(), "dotenv": Mock()}):
    spec = importlib.util.spec_from_file_location(
        "signin_under_test", Path(__file__).resolve().parents[1] / "signin.py"
    )
    signin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(signin)


def mktrip(s, e):
    return {"startTime": s, "endTime": e, "province": "辽宁省",
            "city": "沈阳市", "district": "铁西区"}


class ActiveTripTests(unittest.TestCase):
    T = "2026-10-05"

    def test_inside_inclusive_both_ends(self):
        # 起始日整天算内（00:00 起）；结束日整天算内（23:59 止）
        self.assertIsNotNone(
            signin.active_trip([mktrip("2026-10-05T00:00:00",
                                       "2026-10-07T00:00:00")], self.T))
        self.assertIsNotNone(
            signin.active_trip([mktrip("2026-09-30T00:00:00",
                                       "2026-10-05T00:00:00")], self.T))

    def test_outside(self):
        self.assertIsNone(
            signin.active_trip([mktrip("2026-10-06T00:00:00",
                                       "2026-10-08T00:00:00")], self.T))
        self.assertIsNone(
            signin.active_trip([mktrip("2026-10-02T00:00:00",
                                       "2026-10-04T00:00:00")], self.T))

    def test_latest_trip_used_order_indifferent(self):
        # 乱序多行（已按 startTime 升序），任何行命中都返回
        trips = [mktrip("2026-10-20T00:00:00", "2026-10-25T00:00:00"),
                 mktrip("2026-09-30T00:00:00", "2026-10-07T00:00:00")]
        trip = signin.active_trip(trips, self.T)
        self.assertEqual(trip["startTime"], "2026-09-30T00:00:00")

    def test_malformed_rows_skipped(self):
        for bad in ({}, {"startTime": "garbage", "endTime": "x"},
                    {"startTime": None, "endTime": None}):
            with self.subTest(bad=bad):
                self.assertIsNone(signin.active_trip([bad], self.T))

    def test_empty(self):
        self.assertIsNone(signin.active_trip([], self.T))


if __name__ == "__main__":
    unittest.main(verbosity=2)
