import io
import tempfile
from datetime import date, timedelta
from pathlib import Path

from PIL import Image
import detect


def main():
    cam = {"name": "c", "ip": "10.0.0.1", "user": "u", "password": ""}
    def snap(*_): return None, "no camera"
    events = Path(tempfile.mkdtemp())
    seen = []
    def attrs(img, cls=None):
        seen.append(img.getpixel((img.width // 2, img.height // 2))[0])
        return {"type": "truck" if seen[-1] < 100 else "bus"}
    d = detect.Detector([cam], snap, {}, events_dir=events)
    d.attrs = attrs
    small, large = (0, 0, 20, 20), (0, 0, 50, 50)
    d._track("c", [("truck", .9, small, 1)], Image.new("RGB", (100, 100), (20, 0, 0)))
    d._track("c", [("truck", .9, small, 1)], Image.new("RGB", (100, 100), (20, 0, 0)))
    assert len(seen) == 1 and d.totals["truck"] == 1
    d._track("c", [("truck", .9, large, 1)], Image.new("RGB", (100, 100), (200, 0, 0)))
    d.flush("c")
    assert len(seen) == 2 and d.breakdown["truck"]["type"]["bus"] == 1
    event = next(events.glob("*.jsonl")).read_text().splitlines()[0]
    assert "classification_error" in event
    best_path = next((events / "crops" / date.today().isoformat()).glob("*-best.jpg"))
    assert Image.open(io.BytesIO(best_path.read_bytes())).convert("RGB").getpixel((25, 25))[0] > 100

    old = (date.today() - timedelta(days=detect.EVENTS_KEEP_DAYS + 1)).isoformat()
    od = events / "crops" / old; od.mkdir(parents=True)
    (od / "x-best.jpg").write_bytes(b"best"); (od / "x-front.jpg").write_bytes(b"front"); (od / "x-rear.jpg").write_bytes(b"rear")
    new = events / "crops" / date.today().isoformat(); (new / "new-front.jpg").write_bytes(b"new")
    d._prune_events()
    assert (od / "x-best.jpg").exists() and not (od / "x-front.jpg").exists() and not (od / "x-rear.jpg").exists()
    assert (new / "new-front.jpg").exists()
    outside = Path(tempfile.mkdtemp())
    old_outside = outside / "crops" / old; old_outside.mkdir(parents=True)
    for name in ("x-best.jpg", "x-front.jpg", "x-rear.jpg"): (old_outside / name).write_bytes(b"keep")
    events_alias = events / "events-alias"; events_alias.symlink_to(outside, target_is_directory=True)
    detect.Detector([cam], snap, {}, events_dir=events_alias)._prune_events()
    assert (old_outside / "x-front.jpg").exists()
    crops_target = events / "crops-alias-target"; crops_target.mkdir()
    crops_old = crops_target / old; crops_old.mkdir()
    (crops_old / "x-front.jpg").write_bytes(b"keep")
    events_crops_alias = events / "events-crops-alias"; events_crops_alias.mkdir()
    (events_crops_alias / "crops").symlink_to(crops_target, target_is_directory=True)
    detect.Detector([cam], snap, {}, events_dir=events_crops_alias)._prune_events()
    assert (crops_old / "x-front.jpg").exists()
    print("detect best/prune self-check ok")


if __name__ == "__main__": main()
