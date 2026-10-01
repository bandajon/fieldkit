from contextlib import nullcontext
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import selfloop


class Client:
    fail_key = None

    def __init__(self):
        self.puts = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts[Key] = Body

    def download_file(self, bucket, key, dest):
        if key == self.fail_key:
            raise OSError("mock download failure")
        Path(dest).write_bytes(b"video")


class ClassifyPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "state.json"
        self.champion = root / "champion.pt"
        self.champion.touch()
        self.root = root

    def run_pass(self, keys, journeys, budget=3600, monotonic=None, live=None, save=None,
                 per_pass=1, state=None):
        cl = self.cl = Client()
        detect = SimpleNamespace(classify_segment=lambda *a, **kw: ("2026-09-27", 0, False))
        ingest = SimpleNamespace(config=lambda: {"cameras": []},
                                 cam_and_start=lambda path: ("cam", 1790500000))
        state = state or {"classified": [], "open_days": []}
        with patch.multiple(selfloop, STATE=self.state, CHAMPION=self.champion,
                            VIDEOS=self.root / "videos", CLASSIFY_PER_PASS=per_pass,
                            CLASSIFY_BUDGET=budget, JOURNEYS_EVERY_S=0), \
             patch.object(selfloop, "Lock", nullcontext), \
             patch.object(selfloop, "load_state", return_value=state), \
             patch.object(selfloop, "save_state", side_effect=save or (lambda s: None)), \
             patch.object(selfloop, "r2", return_value=(None, cl, "bucket")), \
             patch.object(selfloop, "recording_keys", return_value=keys), \
             patch.object(selfloop, "published_manifests", return_value=set()), \
             patch.object(selfloop, "live_lane", side_effect=live or (lambda *a: [])), \
             patch.object(selfloop, "publish", return_value=1), \
             patch.object(selfloop, "journeys_pass", side_effect=journeys), \
             patch.object(selfloop, "prune_open_days", side_effect=lambda c, o, d: set()), \
             patch.object(selfloop, "coverage_manifests", return_value={}), \
             patch.dict("sys.modules", {"detect": detect, "ingest_video": ingest}), \
             patch.object(selfloop, "classify_since", return_value="20260927-000000"), \
             patch("time.monotonic", side_effect=monotonic or _Tick()), \
             patch("time.time", return_value=1790500000):
            selfloop.classify_pass()
        return state

    def test_journeys_only_receive_unconsumed_classifications(self):
        keys = ["site1/cam/20260927-110000.mkv", "site1/cam/20260928-110000.mkv",
                "site1/cam/20260929-110000.mkv"]
        seen = []

        def journeys(*args):
            seen.append(set(args[-1]))
            if len(seen) == 1:
                raise RuntimeError("temporary journey failure")
            day = f"2026-09-{25 + len(seen)}"
            return 0, set(), {("RDA-TG-KTB", day)}

        self.run_pass(keys, journeys)
        self.assertEqual(seen, [set(keys[:1]), set(keys[:2]), set(keys[1:]), set(keys[2:])])

    def test_journeys_publish_health_each_run_after_save(self):
        events = []
        with patch.object(selfloop, "publish_health", side_effect=lambda *a: events.append("health")):
            self.run_pass(["site1/cam/20260927-110000.mkv"], lambda *a: (0, set(), set()),
                          save=lambda s: events.append("save"))
        self.assertGreaterEqual(events.count("health"), 2)   # mid-pass journeys + the finally
        first = events.index("health")
        self.assertEqual(events[first - 1], "save")

    def test_real_journey_pass_reports_checked_day_around_failures(self):
        class FakeS3:
            def get_paginator(self, _):
                return self

            def paginate(self, **kwargs):
                return [{"Contents": []}]

            def put_object(self, **kwargs):
                raise OSError("mock write failure")

        cams = [{"name": "cam1", "handoff": {"camera": "cam2"}}]
        day = ("G", "2026-09-27")
        client = FakeS3()
        with patch.object(selfloop, "bucket_keys", side_effect=OSError("mock read failure")):
            early = selfloop.journeys_pass(client, "bucket", {day}, cams, None)
        self.assertEqual(early, (0, {day}, set()))

        with patch.object(selfloop, "bucket_keys", return_value=[]), \
             patch.object(selfloop, "horizon", return_value=({}, 0)), \
             patch.object(selfloop, "cleared", return_value=[]), \
             patch.dict("sys.modules", {"journeys": SimpleNamespace(build=lambda *a: [])}):
            late = selfloop.journeys_pass(client, "bucket", {day}, cams, None)
        self.assertEqual(late, (0, {day}, {day}))

    def test_interrupted_pass_saves_fresh_backlog_and_excludes_failed_keys(self):
        keys = [f"site1/cam/20260927-110{i}00.mkv" for i in range(3)]
        saved = []
        with self.assertRaises(KeyboardInterrupt):
            self.run_pass(keys, lambda *a: (0, set(), set()), live=lambda *a: (_ for _ in ()).throw(
                KeyboardInterrupt()), save=lambda s: saved.append(dict(s.get("classify_backlog", {}))))
        self.assertTrue(any(item.get("left") == 3 and item.get("failed") == 0 for item in saved))

        keys.append("site1/cam/20260927-111000.mkv")
        state = self.run_pass(keys, lambda *a: (0, set(), set()), budget=1,
                              monotonic=_BudgetClock(), per_pass=2)
        self.assertEqual(state["classify_backlog"]["left"], 3)  # one finished; three remain

    def test_failed_segment_is_reported_but_does_not_count_as_backlog(self):
        keys = [f"site1/cam/20260927-110{i}00.mkv" for i in range(3)]
        Client.fail_key = keys[0]
        self.addCleanup(setattr, Client, "fail_key", None)
        state = self.run_pass(keys, lambda *a: (0, set(), set()))
        self.assertEqual(state["classify_backlog"]["failed"], 1)
        self.assertEqual(state["classify_backlog"]["left"], 0)

    def test_raising_loop_still_recounts_backlog_and_saves(self):
        keys = [f"site1/cam/20260927-110{i}00.mkv" for i in range(3)]
        state = {"classified": [], "open_days": [], "classify_backlog": {"at": "x", "left": 12, "failed": 0}}
        saved = []
        with self.assertRaises(RuntimeError):
            self.run_pass(keys, lambda *a: (0, set(), set()), state=state,
                          live=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
                          save=lambda s: saved.append(dict(s.get("classify_backlog", {}))))
        self.assertEqual(state["classify_backlog"]["left"], 3)
        self.assertEqual(saved[-1]["left"], 3)

    def test_caught_up_pass_with_live_segment_is_not_behind(self):
        state = self.run_pass(["site1/cam/20260927-110000.mkv"], lambda *a: (0, set(), set()))
        self.assertEqual(state["classify_backlog"]["left"], 0)
        self.assertFalse(selfloop.classify_behind(state))

    def test_malformed_alert_does_not_lose_the_recount(self):
        state = {"classified": [], "open_days": [], "alerts": ["junk", {"kind": "x"}]}
        self.run_pass(["site1/cam/20260927-110000.mkv"], lambda *a: (0, set(), set()), state=state)
        self.assertEqual(state["classify_backlog"]["left"], 0)
        self.assertEqual(state["alerts"], [])

    def test_alerts_fold_and_health_json(self):
        selfloop.ALERTS.clear()
        selfloop.alert("lost_counts", "RDA-TG-KTB", "dropped a")
        selfloop.alert("lost_counts", "RDA-TG-KTB", "dropped a")   # identical: deduped
        selfloop.alert("late_footage", "other", "late b")
        old = {"at": "2020-01-01T00:00:00Z", "kind": "dead_camera", "gate": "RDA-TG-KTB", "detail": "stale"}
        state = {"classified": [], "open_days": [], "alerts": [old]}
        self.run_pass(["site1/cam/20260927-110000.mkv"], lambda *a: (0, set(), set()), state=state)
        self.assertEqual(sorted(a["kind"] for a in state["alerts"]), ["late_footage", "lost_counts"])
        self.assertEqual(selfloop.ALERTS, [])
        doc = json.loads(self.cl.puts["fieldkit-health/RDA-TG-KTB.json"])
        self.assertEqual(set(doc), {"gate", "updated", "backlog", "backlog_scope", "last_classify", "lag_s",
                                    "oldest_unclassified_s", "newest", "alerts"})
        self.assertEqual(doc["backlog_scope"], "loop")
        self.assertEqual(doc["oldest_unclassified_s"], {"cam": None})
        self.assertEqual([a["kind"] for a in doc["alerts"]], ["lost_counts"])
        self.assertEqual(doc["lag_s"], {"cam": 0})
        self.assertEqual(doc["newest"]["cam"]["recorded"], doc["newest"]["cam"]["classified"])


class _Tick:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return self.value


class _BudgetClock:
    def __init__(self):
        self.values = iter([0, 0, 0, 0, 2, 2, 2, 2, 2, 2, 2, 2])

    def __call__(self):
        return next(self.values, 2)


if __name__ == "__main__":
    unittest.main()
