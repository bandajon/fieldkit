"""Small retention training eligibility self-check; no model jobs or cloud calls."""
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import selfloop


def main():
    root = Path(tempfile.mkdtemp())
    for kind in ("images", "labels", "attrs"):
        (root / "approved" / kind).mkdir(parents=True)
    (root / "reference.txt").write_text("ref\n")
    (root / "approved" / "images" / "good.jpg").write_bytes(b"x")
    (root / "approved" / "labels" / "good.txt").write_text("0 0.5 0.5 0.2 0.2\n")
    (root / "approved" / "attrs" / "good.json").write_text(json.dumps({"0": {"type": "car"}}))
    (root / "approved" / "labels" / "label-only.txt").write_text("0 0 0 1 1\n")
    (root / "approved" / "attrs" / "attrs-only.json").write_text(json.dumps({"0": {"type": "car"}}))
    (root / "approved" / "images" / "ref.jpg").write_bytes(b"x")
    (root / "approved" / "labels" / "ref.txt").write_text("0 0 0 1 1\n")
    fake_train = types.SimpleNamespace(reference=lambda: {"ref"})
    fake_attrs = types.SimpleNamespace(vocab=lambda: {"type": ["car", "bus"]})
    with patch.object(selfloop, "DATASET", root), patch.dict(sys.modules,
            {"train": fake_train, "train_attrs": fake_attrs}):
        detector, attrs = selfloop.retention_training_ids()
    assert detector == {"good"}
    assert attrs == {"good"}
    assert "attrs-only" not in attrs, "attrs without an approved image must not be eligible"
    assert selfloop.should_train(4599, 3599)
    class DS:
        CONFIG = ("config",)
        LEDGERS = ("approved/images",)
        def __init__(self): self.pulls = []
        def pull(self, _cl, _bucket, names): self.pulls.append(names)
    import contextlib
    def invoke(state, ids, attrs_result=True, run_result=None):
        ds = DS(); attrs_calls = []
        with patch.object(selfloop, "THRESHOLD", 2), \
             patch.object(selfloop, "Lock", lambda: contextlib.nullcontext()), \
             patch.object(selfloop, "load_state", return_value=state), \
             patch.object(selfloop, "save_state"), \
             patch.object(selfloop, "r2", return_value=(ds, object(), "bucket")), \
             patch.object(selfloop, "retention_policy", return_value={"active": True}), \
             patch.object(selfloop, "retention_training_ids", return_value=ids), \
             patch.object(selfloop, "attrs_pass", side_effect=lambda *a: attrs_calls.append(1) or attrs_result), \
             patch("subprocess.run", return_value=run_result) as run:
            selfloop.train_pass()
        return ds, attrs_calls, run

    # Migration records the current corpus and never launches a job.
    state = {"trained_frames": 3599}
    ds, calls, run = invoke(state, ({"old", "new"}, {"a", "b"}))
    assert state["retention_detector_source_ids"] == ["new", "old"]
    assert state["retention_attrs_source_ids"] == ["a", "b"]
    assert not run.called and not calls and state["trained_frames"] == 3599

    # Attribute-only growth still runs when the raw approved count has shrunk.
    state = {"trained_frames": 3599, "retention_detector_source_ids": ["old"],
             "retention_attrs_source_ids": []}
    ds, calls, run = invoke(state, ({"old"}, {"a", "b"}))
    assert calls == [1] and not run.called and state["retention_attrs_source_ids"] == ["a", "b"]
    before = list(state["retention_attrs_source_ids"])
    ds, calls, run = invoke(state, ({"old"}, {"a", "b", "c", "d"}), attrs_result=False)
    assert calls == [1] and state["retention_attrs_source_ids"] == before

    # A failed detector does not prevent the independent attribute attempt.
    state = {"trained_frames": 3599, "retention_detector_source_ids": [],
             "retention_attrs_source_ids": []}
    failed = type("Result", (), {"returncode": 1})()
    ds, calls, run = invoke(state, ({"d1", "d2"}, {"a", "b"}), run_result=failed)
    assert calls == [1] and run.called
    assert state["retention_detector_source_ids"] == []
    assert state["retention_attrs_source_ids"] == ["a", "b"]
    print("retention training eligibility self-check ok")


if __name__ == "__main__":
    main()
