"""Attribute labeller checks; temporary data only, mocked model calls."""
import json
import io
import os
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from PIL import Image
import yaml

import suggest_attrs as labeller
import dataset_retention


def fixture(root, mode="approved", count=1, box="0.512345 0.5 0.4 0.3", cls=0):
    root.joinpath("attributes.yaml").write_text(yaml.safe_dump({
        "colour": ["white", "blue"], "make": ["Volvo", "Higer"],
        "axles": ["2", "3"],
        "constraints": {"b-light": {"make": ["Volvo"]}},
    }))
    root.joinpath("classes.txt").write_text("e-heavy\nb-light\na-motorcycle\ne-plant\n")
    for folder in ("images", "labels", "attrs", "suggest"):
        (root / mode / folder).mkdir(parents=True)
    Image.new("RGB", (1000, 800), "white").save(root / mode / "images/sample.jpg")
    (root / mode / "labels/sample.txt").write_text((f"{cls} {box}\n") * count)


def fake_runner(haiku, sonnet, calls):
    def run(model, prompt):
        crops = json.loads(prompt.split("Crops:\n", 1)[1])
        assert 1 <= len(crops) <= 10
        for crop in crops:
            assert Path(crop["path"]).is_absolute()
            assert Path(crop["path"]).is_file()
            assert set(crop["values"]) <= {"colour", "make"}
        calls.append((model, len(crops)))
        answer = haiku if model == "haiku" else sonnet
        return "Here are the results:\n```json\n" + json.dumps([
            {"id": crop["id"], **answer} for crop in crops
        ]) + "\n```"
    return run


def run_case(haiku=None, sonnet=None, count=1,
             box="0.512345 0.5 0.4 0.3", cls=0, human=None, limit=None):
    good = {"colour": "white", "make": "Volvo"}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture(root, "approved", count, box, cls)
        if human:
            (root / "approved" / "attrs/sample.json").write_text(json.dumps({"0": human}))
        calls = []
        runner = fake_runner(good if haiku is None else haiku,
                             good if sonnet is None else sonnet, calls)
        with patch.object(labeller, "DATASET", root), patch.object(labeller, "PENDING", root / "pending"), \
                redirect_stdout(io.StringIO()) as progress:
            labeller.run_claude(limit=limit, runner=runner)
            ledger = root / "ai-attrs"
            files = sorted(ledger.glob("*.jsonl"))
            rows = [json.loads(line) for file in files for line in file.read_text().splitlines()]
            assert len(files) == bool(rows), "one run must produce exactly one file if it labelled anything"
            assert not (root / "ai-attrs.jsonl").exists()
            path = root / "approved" / "suggest/sample.json"
            suggestions = json.loads(path.read_text()) if path.exists() else {}
            assert "suggested" in progress.getvalue()
            if limit is None and rows:
                before = len(calls)
                ledger_before = {f.name: f.read_bytes() for f in files}
                suggestions_before = path.read_bytes() if path.exists() else None
                labeller.run_claude(runner=runner)
                assert len(calls) == before, "rerun must not call models"
                assert {f.name: f.read_bytes() for f in ledger.glob("*.jsonl")} == ledger_before
                assert (path.read_bytes() if path.exists() else None) == suggestions_before
        return rows, suggestions, calls


def boundary_checks():
    items = [{"id": "0", "values": {"colour": ["white"]}}]
    assert labeller.claude_answers('prose [unreadable] [{"id":"0","colour":"white"}]', items, {}) == {
        "0": {"colour": "white"}}
    assert labeller.claude_answers('[{"id":"other","colour":"white"}]', items, {}) == {}
    assert labeller.claude_answers('[{"id":"0","colour":"white"},{"id":"0","colour":"white"}]', items, {}) == {}
    try:
        labeller.claude_answers("not JSON", items, {})
    except ValueError:
        pass
    else:
        assert False, "malformed output must be rejected"
    for change in ("expired", "box", "human"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture(root)
            blocked, parallel, barrier, calls = Event(), Event(), Barrier(2), []
            fake = fake_runner({"colour": "white", "make": "Volvo"},
                               {"colour": "white", "make": "Volvo"}, calls)

            def runner(model, prompt):
                answer = fake(model, prompt)
                if model == "sonnet":
                    if change == "expired":
                        blocked.set()
                    elif change == "box":
                        (root / "approved" / "labels/sample.txt").write_text("0 0.4 0.5 0.4 0.3\n")
                    else:
                        (root / "approved" / "attrs/sample.json").write_text(
                            json.dumps({"0": {"colour": "blue", "make": "Higer"}}))
                barrier.wait(timeout=5)  # Both models must be running concurrently.
                parallel.set()
                return answer

            with patch.object(labeller, "DATASET", root), \
                    patch.object(dataset_retention, "cached_policy", return_value={"test": True}), \
                    patch.object(dataset_retention, "expired", side_effect=lambda *args: blocked.is_set()), \
                    redirect_stdout(io.StringIO()):
                labeller.run_claude(runner=runner)
            assert sorted(calls) == [("haiku", 1), ("sonnet", 1)]
            assert parallel.is_set(), "both model calls must meet at the barrier"
            assert not list((root / "ai-attrs").glob("*"))
            assert not (root / "approved" / "suggest/sample.json").exists()


def run_file_checks():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture(root)
        calls = []
        runner = fake_runner({"colour": "white", "make": "Volvo"},
                             {"colour": "white", "make": "Volvo"}, calls)
        with patch.object(labeller, "DATASET", root), redirect_stdout(io.StringIO()):
            labeller.run_claude(runner=runner)
            files = list((root / "ai-attrs").glob("*.jsonl"))
            assert len(files) == 1, "approved run must create one immutable run file"
            first = files[0]
            original = first.read_bytes()
            labeller.run_claude(runner=runner)
            assert len(list(first.parent.iterdir())) == 1 and len(calls) == 2
            (root / "approved/labels/sample.txt").write_text("0 0.4 0.5 0.4 0.3\n")
            labeller.run_claude(runner=runner)
            assert len(list(first.parent.iterdir())) == 2 and len(calls) == 4
            assert first.read_bytes() == original, "earlier run must remain immutable"
            assert labeller.ai_entries()[("sample", 0)]["bbox"] == [0.4, 0.5, 0.4, 0.3]
            # Creation order and 'at' must not override filename/line order.
            latest = {"sid": "sample", "box": 0, "bbox": [0.3, 0.5, 0.4, 0.3], "at": "old"}
            (first.parent / "99991231T235959Z.jsonl").write_text(
                json.dumps(latest) + "\n" + json.dumps({**latest, "attrs": {"make": "Higer"}}) + "\n")
            (first.parent / "00010101T000000Z.jsonl").write_text(json.dumps({**latest, "at": "new"}) + "\n")
            assert labeller.ai_entries()[("sample", 0)] == {**latest, "attrs": {"make": "Higer"}}
            (root / "approved/labels/sample.txt").write_text("0 0.3 0.5 0.4 0.3\n")
            before = len(calls)
            labeller.run_claude(runner=runner)
            assert len(calls) == before, "rerun skip must use the last entry across all run files"


def atomic_publication_checks():
    for fail in (None, "write", "publish"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fixture(root, count=2)
            directory = root / "ai-attrs"
            directory.mkdir()
            finished = directory / "20000101T000000Z.jsonl"
            finished.write_text('{"sid":"old","box":0}\n')
            original = finished.read_bytes()
            staged, published = [], []
            new_temp, replace = tempfile.NamedTemporaryFile, os.replace

            def temporary_file(*args, **kwargs):
                ledger = new_temp(*args, **kwargs)
                staged.append(Path(ledger.name))
                write = ledger.writelines

                def writelines(lines):
                    assert set(directory.iterdir()) == {finished}, "unfinished ledger must stay outside synced ai-attrs"
                    assert staged[-1].parent == root and staged[-1].parent != directory, "stage at dataset root"
                    lines = iter(lines)
                    write([next(lines)])
                    ledger.flush()
                    assert set(directory.iterdir()) == {finished}, "partial ledger must stay outside synced ai-attrs"
                    if fail == "write":
                        raise OSError("injected write failure")
                    write(lines)

                ledger.writelines = writelines
                return ledger

            def publish(source, dest):
                source, dest = Path(source), Path(dest)
                assert source == staged[-1] and source.parent == root
                assert dest.parent == directory and not dest.exists()
                contents = source.read_bytes()
                assert len(contents.splitlines()) == 2
                assert all(json.loads(line)["sid"] == "sample" for line in contents.splitlines())
                assert set(directory.iterdir()) == {finished}
                if fail == "publish":
                    raise OSError("injected publish failure")
                replace(source, dest)
                assert dest.read_bytes() == contents and not source.exists()
                published.append(dest)

            runner = fake_runner({"colour": "white", "make": "Volvo"},
                                 {"colour": "white", "make": "Volvo"}, [])
            with patch.object(labeller, "DATASET", root), \
                    patch.object(tempfile, "NamedTemporaryFile", side_effect=temporary_file), \
                    patch.object(os, "replace", side_effect=publish), redirect_stdout(io.StringIO()):
                try:
                    labeller.run_claude(runner=runner)
                except OSError as exc:
                    assert fail and str(exc) == f"injected {fail} failure"
                else:
                    assert fail is None
            assert staged and all(not path.exists() for path in staged), "staging files must be cleaned up"
            assert len(published) == (fail is None), "successful publication must use os.replace"
            assert set(directory.iterdir()) == {finished, *published}
            assert finished.read_bytes() == original


def ollama_main_checks():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fixture(root, "pending", count=2)
        out = root / "pending/suggest/sample.json"
        out.write_text('{"0":{"axles":"3"}}')
        requests = []

        def urlopen(request, timeout):
            requests.append(request)
            if isinstance(request, str):
                assert request == labeller.OLLAMA + "/api/tags" and timeout == 5
                return io.BytesIO(b"{}")
            assert request.full_url == labeller.OLLAMA + "/api/chat"
            body = json.loads(request.data)
            assert body["model"] == labeller.MODEL and body["messages"][0]["images"]
            assert timeout == labeller.TIMEOUT_S
            return io.BytesIO(json.dumps({"message": {"content": json.dumps({
                "colour": "white", "make": "Volvo", "axles": "2", "invalid": "x"})}}).encode())

        with patch.object(labeller, "DATASET", root), \
                patch.object(labeller, "PENDING", root / "pending"), \
                patch.object(labeller.sys, "argv", ["suggest_attrs.py"]), \
                patch.object(labeller.urllib.request, "urlopen", side_effect=urlopen), \
                redirect_stdout(io.StringIO()) as progress:
            labeller.main()
            assert json.loads(out.read_text()) == {
                "0": {"axles": "3"}, "1": {"colour": "white", "make": "Volvo", "axles": "2"}}
            assert len(requests) == 2 and "1 suggested, 0 skipped" in progress.getvalue()
            labeller.main()
            assert len(requests) == 2, "completed pending samples must not request Ollama again"
        assert not (root / "ai-attrs").exists()


def final_retention_checks():
    for all_expired in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture(root, count=10)
            for folder, suffix in (("images", ".jpg"), ("labels", ".txt")):
                source = root / "approved" / folder / ("sample" + suffix)
                dest = source.with_name("z-live" + suffix)
                dest.write_bytes(source.read_bytes())
            (root / "approved/labels/z-live.txt").write_text("0 0.5 0.5 0.4 0.3\n")
            expired = Event()
            calls = []
            fake = fake_runner({"colour": "white", "make": "Volvo"},
                               {"colour": "white", "make": "Volvo"}, calls)

            def runner(model, prompt):
                answer = fake(model, prompt)
                if model == "sonnet" and len(json.loads(prompt.split("Crops:\n", 1)[1])) == 1:
                    expired.set()  # Batch one's rows are already buffered.
                return answer

            with patch.object(labeller, "DATASET", root), \
                    patch.object(dataset_retention, "cached_policy", side_effect=lambda root:
                                 {"test": True} if expired.is_set() else None), \
                    patch.object(dataset_retention, "expired", side_effect=lambda sid, policy:
                                 sid == "sample" or all_expired), \
                    redirect_stdout(io.StringIO()):
                labeller.run_claude(runner=runner)
            assert sorted(calls) == [("haiku", 1), ("haiku", 10), ("sonnet", 1), ("sonnet", 10)]
            files = list((root / "ai-attrs").glob("*"))
            if all_expired:
                assert files == [], "all expired buffered rows must produce no run file"
            else:
                assert len(files) == 1 and files[0].suffix == ".jsonl"
                rows = [json.loads(line) for line in files[0].read_text().splitlines()]
                assert [row["sid"] for row in rows] == ["z-live"], "batch-one expired sample must not be published"


def claude_cli_checks():
    with patch.object(labeller, "run_claude") as run:
        for arguments, limit in (([], None), (["--limit", "12"], 12)):
            with patch.object(labeller.sys, "argv", ["suggest_attrs.py", "claude", *arguments]):
                labeller.main()
            run.assert_called_once_with(limit)
            run.reset_mock()
        for arguments in (["pending"], ["approved"], ["--limit", "-1"]):
            with patch.object(labeller.sys, "argv", ["suggest_attrs.py", "claude", *arguments]), \
                    patch.object(labeller.sys, "stderr", io.StringIO()):
                try:
                    labeller.main()
                except SystemExit as exc:
                    assert exc.code == 2
                else:
                    assert False, "invalid Claude arguments must be rejected"
            run.assert_not_called()


def main():
    atomic_publication_checks()
    ollama_main_checks()
    claude_cli_checks()
    final_retention_checks()
    run_file_checks()
    rows, _, _ = run_case()
    assert len(rows) == 1
    assert rows[0]["sid"] == "sample" and rows[0]["box"] == 0
    assert rows[0]["bbox"] == [0.512345, 0.5, 0.4, 0.3]
    assert rows[0]["attrs"] == {"colour": "white", "make": "Volvo"}
    assert rows[0]["models"] == ["haiku", "sonnet"]
    assert rows[0]["at"].endswith("+00:00")
    assert run_case(sonnet={"colour": "blue", "make": "Higer"})[0] == []
    rows, _, _ = run_case(sonnet={"colour": "blue", "make": "Volvo"})
    assert rows[0]["attrs"] == {"make": "Volvo"}
    assert run_case(haiku={"colour": "purple", "make": "unknown"},
                    sonnet={"colour": "purple", "make": "unknown"})[0] == []
    assert run_case(cls=1, haiku={"make": "Higer"}, sonnet={"make": "Higer"})[0] == []
    rows, _, _ = run_case(human={"colour": "blue"})
    assert rows[0]["attrs"] == {"make": "Volvo"}
    for kwargs in ({"box": "0.5 0.5 0.089 0.3"}, {"box": "0.5 0.5 0.4 0.074"},
                   {"cls": 2}, {"cls": 3}, {"human": {"colour": "blue", "make": "Volvo"}}):
        rows, _, calls = run_case(**kwargs)
        assert rows == [] and calls == []
    rows, _, calls = run_case(count=23, limit=12)
    assert len(rows) == 12
    assert sorted(calls[:4]) == sorted([("haiku", 10), ("sonnet", 10), ("haiku", 2), ("sonnet", 2)])
    rows, _, calls = run_case(count=23)
    assert len(rows) == 23 and len(calls) == 6
    assert run_case(limit=0)[2] == []
    boundary_checks()
    print("test_suggest_attrs: all checks passed")


if __name__ == "__main__":
    main()
