#!/usr/bin/env python3
"""Small dependency-light archive/training regression check."""
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import classifier_crops as cc
import train_attrs


def main():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td); (d / "approved/images").mkdir(parents=True); (d / "approved/labels").mkdir(); (d / "approved/attrs").mkdir()
        (d / "classes.txt").write_text("car\ntruck\n")
        sid = "cam-20260909-120000"
        im0 = Image.new("RGB", (20, 10)); im0.putdata([(x * 11 % 255, y * 23 % 255, (x + y) * 17 % 255) for y in range(10) for x in range(20)]); im0.save(d / "approved/images" / f"{sid}.jpg")
        (d / "approved/labels" / f"{sid}.txt").write_text("1 0.5 0.5 0.5 0.5\n")
        (d / "approved/attrs" / f"{sid}.json").write_text(json.dumps({"0": {"type": "van"}}))
        m = cc.export_sample(d, sid)
        assert not list(d.glob(".classifier-crops-*")), "successful export leaked staging"
        assert len(m["samples"]) == 1 and m["capture_utc"].endswith("+00:00")
        s = m["samples"][0]; p = d / cc.ROOT_NAME / sid / s["crop"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == s["crop_sha256"]
        with Image.open(d / "approved/images" / f"{sid}.jpg") as src, Image.open(p) as got:
            assert list(got.getdata()) == list(train_attrs.crop_box(src.convert("RGB"), s["bbox"]).getdata())
        assert cc.verified_archive(d, sid)["source_sid"] == sid
        revs = sorted((d / cc.ROOT_NAME / sid).glob("[0-9a-f]*.json"))
        cc.export_sample(d, sid)
        assert sorted((d / cc.ROOT_NAME / sid).glob("[0-9a-f]*.json")) == revs
        old = p.read_bytes(); (p).write_bytes(b"tampered")
        try: cc.verified_archive(d, sid); raise AssertionError("tamper accepted")
        except ValueError: pass
        p.write_bytes(old)
        (d / "reference.txt").write_text(sid + "\n")
        assert cc.export_sample(d, sid)["reference"] is True
        sid2 = "plain"
        Image.new("RGB", (20, 10), (1, 2, 3)).save(d / "approved/images" / f"{sid2}.jpg")
        (d / "approved/labels" / f"{sid2}.txt").write_text("0 0.5 0.5 0.5 0.5\n")
        assert cc.export_sample(d, sid2)["source_sha256"]["attrs"] is None
        (d / "approved/labels" / f"{sid2}.txt").write_text("0 2 0.5 0.5 0.5\n")
        try: cc.export_sample(d, sid2); raise AssertionError("bad box accepted")
        except ValueError: pass
        (d / "approved/attrs" / f"{sid2}.json").write_text('{"9": {"type": "van"}}')
        (d / "approved/labels" / f"{sid2}.txt").write_text("0 0.5 0.5 0.5 0.5\n")
        try: cc.export_sample(d, sid2); raise AssertionError("bad attrs accepted")
        except ValueError: pass
        try: cc.export_sample(d, "../escape"); raise AssertionError("unsafe SID accepted")
        except ValueError: pass
        rev = next((d / cc.ROOT_NAME / sid).glob("[0-9a-f]*.json")); rev_bytes = rev.read_bytes(); rev.write_bytes(b"bad")
        try: cc.verified_archive(d, sid); raise AssertionError("bad revision accepted")
        except ValueError: pass
        rev.write_bytes(rev_bytes)
        train_attrs.DATASET = d; train_attrs.APPROVED = d / "approved"; train_attrs.BASELINE = True
        heads = {"type": ["van", "truck"]}
        # An exported non-reference SID remains usable after retention, unless it is later added to reference.txt.
        sid5 = "retained-20260909-120000"
        Image.new("RGB", (20, 10), (9, 8, 7)).save(d / "approved/images" / f"{sid5}.jpg")
        (d / "approved/labels" / f"{sid5}.txt").write_text("0 0.5 0.5 0.5 0.5\n")
        (d / "approved/attrs" / f"{sid5}.json").write_text('{"0":{"type":"van"}}')
        cc.export_sample(d, sid5)
        for kind, suffix in (("images", ".jpg"), ("labels", ".txt"), ("attrs", ".json")):
            (d / "approved" / kind / f"{sid5}{suffix}").unlink()
        train_attrs.BASELINE = False
        (d / "reference.txt").write_text(sid5 + "\n")
        assert sid5 not in train_attrs.build(heads)[2]
        train_attrs.BASELINE = True
        assert sid5 in train_attrs.build(heads)[2]
        crops, targets, stems, classes = train_attrs.build(heads)
        assert set(stems) == {sid, sid5} and len(crops) == 2
        (d / "approved/images" / f"{sid}.jpg").unlink()
        (d / "approved/labels" / f"{sid}.txt").unlink()
        (d / "approved/attrs" / f"{sid}.json").unlink()
        train_attrs.BASELINE = False
        crops, targets, stems, classes = train_attrs.build(heads)
        assert not crops
        train_attrs.BASELINE = True
        crops, targets, stems, classes = train_attrs.build(heads)
        assert set(stems) == {sid, sid5} and len(crops) == 2
        # taxonomy is resolved by stable archived class name
        (d / "classes.txt").write_text("truck\ncar\n")
        assert train_attrs.build(heads)[3] == [0, 1]
        # a present current SID suppresses archived boxes, including cleared/removed rows
        sid3 = "two-20260909-120000"
        Image.new("RGB", (20, 10), (4, 5, 6)).save(d / "approved/images" / f"{sid3}.jpg")
        (d / "approved/labels" / f"{sid3}.txt").write_text("0 .5 .5 .2 .2\n1 .4 .4 .2 .2\n")
        (d / "approved/attrs" / f"{sid3}.json").write_text('{"0":{"type":"van"},"1":{"type":"van"}}')
        cc.export_sample(d, sid3)
        (d / "approved/labels" / f"{sid3}.txt").write_text("0 .5 .5 .2 .2\n")
        (d / "approved/attrs" / f"{sid3}.json").write_text('{}')
        assert sid3 not in train_attrs.build(heads)[2]
        # reference membership survives pruning and re-export
        Image.new("RGB", (20, 10), (10, 20, 30)).save(d / "approved/images" / f"{sid}.jpg")
        (d / "approved/labels" / f"{sid}.txt").write_text("1 .5 .5 .5 .5\n")
        (d / "approved/attrs" / f"{sid}.json").write_text('{"0":{"type":"van"}}')
        (d / "reference.txt").unlink(); assert cc.export_sample(d, sid)["reference"] is True
        train_attrs.BASELINE = False
        assert sid not in train_attrs.build({"type": ["van", "truck"]})[2]
        # source mutation during crop generation blocks publication
        sid4 = "mutate-20260909-120000"
        Image.new("RGB", (20, 10), (7, 8, 9)).save(d / "approved/images" / f"{sid4}.jpg")
        (d / "approved/labels" / f"{sid4}.txt").write_text("0 .5 .5 .2 .2\n")
        (d / "approved/attrs" / f"{sid4}.json").write_text('{"0":{"type":"van"}}')
        original = train_attrs.crop_box
        def mutate(*args):
            (d / "approved/attrs" / f"{sid4}.json").unlink(); return original(*args)
        with patch.object(train_attrs, "crop_box", mutate):
            try: cc.export_sample(d, sid4); raise AssertionError("mutation accepted")
            except ValueError: pass
        assert not (d / cc.ROOT_NAME / sid4 / "manifest.json").exists()
        assert not list(d.glob(".classifier-crops-*")), "failed export leaked staging"
        surviving_stems = train_attrs.build({"type": ["van", "truck"]})[2]
        assert sid5 in surviving_stems or sid in surviving_stems
        # A failure moving the initial stage into place publishes no partial SID and preserves sources.
        sid6 = "replace-failure-20260909-120000"
        source_files = {}
        Image.new("RGB", (20, 10), (6, 5, 4)).save(d / "approved/images" / f"{sid6}.jpg")
        (d / "approved/labels" / f"{sid6}.txt").write_text("0 .5 .5 .2 .2\n")
        (d / "approved/attrs" / f"{sid6}.json").write_text('{"0":{"type":"van"}}')
        for kind, suffix in (("images", ".jpg"), ("labels", ".txt"), ("attrs", ".json")):
            source_files[kind] = (d / "approved" / kind / f"{sid6}{suffix}").read_bytes()
        original_replace = cc.os.replace
        def fail_initial_stage(src, dst):
            if Path(src).name == sid6 and Path(dst) == d / cc.ROOT_NAME / sid6:
                raise OSError("injected initial stage publication failure")
            return original_replace(src, dst)
        with patch.object(cc.os, "replace", side_effect=fail_initial_stage):
            try: cc.export_sample(d, sid6); raise AssertionError("injected replace failure accepted")
            except OSError: pass
        assert not (d / cc.ROOT_NAME / sid6).exists()
        assert not list(d.glob(".classifier-crops-*"))
        assert all((d / "approved" / kind / f"{sid6}{suffix}").read_bytes() == source_files[kind]
                   for kind, suffix in (("images", ".jpg"), ("labels", ".txt"), ("attrs", ".json")))
        surviving_stems = train_attrs.build({"type": ["van", "truck"]})[2]
        assert sid5 in surviving_stems or sid in surviving_stems
        # source and archive symlinks are rejected, including dangling archive roots
        real = d / "approved/images-real"; (d / "approved/images").rename(real); (d / "approved/images").symlink_to(real, target_is_directory=True)
        try: cc.export_sample(d, sid2); raise AssertionError("source symlink accepted")
        except ValueError: pass
        (d / "approved/images").unlink(); real.rename(d / "approved/images")
        ar = d / cc.ROOT_NAME; ar.rename(d / "archive-real"); ar.symlink_to(d / "missing-archive", target_is_directory=True)
        try: cc.export_sample(d, sid2); raise AssertionError("archive symlink accepted")
        except ValueError: pass
        print("ok — archive integrity, retention, staging cleanup, and publish-failure checks")


if __name__ == "__main__":
    main()
