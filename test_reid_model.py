"""Synthetic TorchScript fixtures only; no trained weights or camera data."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import torch
from PIL import Image

import detect


class SyntheticReid(torch.nn.Module):
    version: str

    def __init__(self):
        super().__init__()
        self.version = "synthetic-v1"
        self.register_buffer("preprocess_mean", torch.tensor((.48145466, .4578275, .40821073)))
        self.register_buffer("preprocess_std", torch.tensor((.26862954, .26130258, .27577711)))

    def forward(self, x):
        return torch.nn.functional.normalize(torch.ones((x.size(0), 256)), dim=1)


class ReidModelTest(unittest.TestCase):
    def test_clip_buffers_load(self):
        with tempfile.TemporaryDirectory() as td:
            weights = Path(td) / "synthetic.pt"
            torch.jit.script(SyntheticReid()).save(str(weights))
            error = Mock()
            self.assertIsNotNone(detect.reid_model(weights, "cpu", error))
            error.assert_not_called()

    def test_mismatched_buffers_disable_reid_and_report_error(self):
        for name in ("preprocess_mean", "preprocess_std"):
            with self.subTest(buffer=name), tempfile.TemporaryDirectory() as td:
                model = SyntheticReid()
                getattr(model, name).add_(.1)
                weights = Path(td) / "synthetic.pt"
                torch.jit.script(model).save(str(weights))
                events = Path(td) / "events"
                detector = detect.Detector([{"name": "c"}], lambda *a: None,
                                           {"reid_weights": str(weights)}, events_dir=events)
                with patch.dict(sys.modules, {"ultralytics": SimpleNamespace(YOLO=None)}), \
                     patch("torch.backends.mps.is_available", return_value=False):
                    detector._load()
                self.assertIsNone(detector.reid)
                self.assertIn("re-ID:", detector.info()["error"])
                self.assertIn(name, detector.error)
                self.assertIn("CLIP", detector.error)
                detector.display["truck"] = "e-heavy"
                detector.totals["e-heavy"] = 0
                with patch.object(detect, "reid_code") as inference:
                    for _ in range(detect.TRACKLET_CROP_HITS):
                        detector._track("c", [("truck", .9, (0., 0., 200., 100.), 1)],
                                        Image.new("RGB", (300, 200)))
                    detector.flush("c")
                inference.assert_not_called()
                rows = [json.loads(line) for f in (events / "tracklets").glob("*.jsonl")
                        for line in f.read_text().splitlines()]
                self.assertEqual(len(rows), 1)
                self.assertNotIn("reid", rows[0])
                self.assertNotIn("reid_v", rows[0])


if __name__ == "__main__":
    unittest.main()
