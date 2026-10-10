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
    def run(model, prompt, cwd):
        crops = json.loads(prompt.split("Crops:\n", 1)[1])
        assert 1 <= len(crops) <= 10
        for crop in crops:
            assert Path(crop["path"]).is_absolute()
            assert Path(crop["path"]).is_file()
            assert Path(crop["path"]).parent == Path(cwd).resolve()
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
            assert len(files) == (len(rows) + 9) // 10, "one immutable file per completed batch"
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

            def runner(model, prompt, cwd):
                answer = fake(model, prompt, cwd)
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

            def runner(model, prompt, cwd):
                answer = fake(model, prompt, cwd)
                if model == "sonnet" and len(json.loads(prompt.split("Crops:\n", 1)[1])) == 1:
                    expired.set()  # Batch one's rows are already published.
                return answer

            with patch.object(labeller, "DATASET", root), \
                    patch.object(dataset_retention, "cached_policy", side_effect=lambda root:
                                 {"test": True} if expired.is_set() else None), \
                    patch.object(dataset_retention, "expired", side_effect=lambda sid, policy:
                                 sid == "sample" or all_expired), \
                    redirect_stdout(io.StringIO()):
                labeller.run_claude(runner=runner)
            assert sorted(calls) == [("haiku", 1), ("haiku", 10), ("sonnet", 1), ("sonnet", 10)]
            files = sorted((root / "ai-attrs").glob("*.jsonl"))
            rows = [json.loads(line) for file in files for line in file.read_text().splitlines()]
            assert len(files) == (1 if all_expired else 2)
            assert [row["sid"] for row in rows] == ["sample"] * 10 + ([] if all_expired else ["z-live"]), "completed batches remain immutable; final batch rechecks retention"


def claude_cli_checks():
    with patch.object(labeller.shutil, "which", return_value="/mock/claude"), patch.object(labeller, "run_claude") as run:
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


def review_parser():
    items = [{"id": "0", "values": {"colour": ["white"]}}]
    assert labeller.claude_answers('["prose"] [{"id":0,"colour":"white"},{"id":"invented","colour":"white"}]', items, {}) == {"0": {"colour": "white"}}


def review_malformed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); (root / "ai-attrs").mkdir()
        (root / "ai-attrs/run.jsonl").write_text('bad\n{}\n[]\n{"sid":"ok","box":0}\n')
        with patch.object(labeller, "DATASET", root):
            assert labeller.ai_entries() == {("ok", 0): {"sid": "ok", "box": 0}}


def review_interrupt():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); fixture(root, count=11)
        calls = []
        fake = fake_runner({"colour": "white"}, {"colour": "white"}, calls)
        def runner(model, prompt, cwd):
            if len(json.loads(prompt.split("Crops:\n", 1)[1])) == 1:
                raise KeyboardInterrupt()
            return fake(model, prompt, cwd)
        with patch.object(labeller, "DATASET", root), redirect_stdout(io.StringIO()):
            try:
                labeller.run_claude(runner=runner)
            except KeyboardInterrupt:
                pass
        rows = [json.loads(line) for file in (root / "ai-attrs").glob("*.jsonl") for line in file.read_text().splitlines()]
        assert len(rows) == 10, "completed first batch survives interruption"


def review_cli_isolation():
    prompt = 'Prompt without crop metadata'
    cwd = Path('/tmp/mock-batch')
    with patch.object(labeller.subprocess, 'run') as run:
        run.return_value.stdout = '[]'
        for model in ('haiku', 'sonnet'):
            labeller.claude_runner(model, prompt, cwd)
            args, kwargs = run.call_args
            assert args[0] == ['claude', '-p', '--model', model, '--allowedTools', 'Read', '--setting-sources', 'project', '--strict-mcp-config']
            assert kwargs['cwd'] == cwd and kwargs['input'] == prompt
            if os.name == 'nt':
                assert kwargs['creationflags'] == labeller.subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                assert kwargs['start_new_session'] is True
    with patch.object(labeller.shutil, 'which', return_value=None), patch.object(labeller, 'run_claude') as run, patch.object(labeller.sys, 'argv', ['suggest_attrs.py', 'claude']), patch.object(labeller.sys, 'stderr', io.StringIO()) as error:
        try:
            labeller.main()
        except SystemExit as exc:
            assert exc.code == 1 and 'not found on PATH' in error.getvalue()
        else:
            assert False, 'missing binary must fail startup'
        run.assert_not_called()
    rows, _, _ = run_case(haiku={}, sonnet={})
    assert not rows
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); fixture(root)
        with patch.object(labeller, 'DATASET', root), redirect_stdout(io.StringIO()) as out:
            labeller.run_claude(runner=lambda *a: 'prose ["no answer"]')
        assert len([line for line in out.getvalue().splitlines() if 'warning' in line]) == 1


def review_current_batch_interrupt():
    from concurrent.futures import Future
    original = Future.result
    interrupted = []
    def result(future, *args, **kwargs):
        if not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt()
        return original(future, *args, **kwargs)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); fixture(root, count=11)
        calls = []
        with patch.object(labeller, 'DATASET', root), patch.object(Future, 'result', result), redirect_stdout(io.StringIO()):
            labeller.run_claude(runner=fake_runner({'colour': 'white'}, {'colour': 'white'}, calls))
        rows = [json.loads(line) for file in (root / 'ai-attrs').glob('*.jsonl') for line in file.read_text().splitlines()]
        assert len(rows) == 10 and len(calls) == 2, 'interrupt waits for current batch publication, then stops'


def review_empty_values_warning():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td); fixture(root)
        fake = fake_runner({'colour': 'unknown'}, {'colour': 'unknown'}, [])
        with patch.object(labeller, 'DATASET', root), redirect_stdout(io.StringIO()) as out:
            labeller.run_claude(runner=fake)
        assert len([line for line in out.getvalue().splitlines() if 'warning' in line]) == 1, 'empty cleaned answers need one warning'


def review_signal_seams():
    import signal
    for seam in ('parser', 'publish'):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); fixture(root, count=11)
            calls, raised = [], []
            original = labeller.claude_answers if seam == 'parser' else os.replace
            def interrupt(*args, **kwargs):
                if not raised:
                    raised.append(True)
                    signal.raise_signal(signal.SIGINT)
                return original(*args, **kwargs)
            previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
            target = patch.object(labeller, 'claude_answers', side_effect=interrupt) if seam == 'parser' else patch.object(os, 'replace', side_effect=interrupt)
            with patch.object(labeller, 'DATASET', root), target, redirect_stdout(io.StringIO()):
                try:
                    labeller.run_claude(runner=fake_runner({'colour': 'white'}, {'colour': 'white'}, calls))
                except KeyboardInterrupt:
                    pass
            assert {sig: signal.getsignal(sig) for sig in previous} == previous, 'restore original signal handlers'
            rows = [json.loads(line) for file in (root / 'ai-attrs').glob('*.jsonl') for line in file.read_text().splitlines()]
            assert len(rows) == 10 and len(calls) == 2, f'{seam} interrupt must publish current batch then stop'


def review_terminal_process_group():
    import signal
    import subprocess
    import sys
    import time
    if os.name == 'nt':
        return  # POSIX terminal signal reproduction; Windows flag is asserted below.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        ready = root / 'ready'
        child = "import signal,time; from pathlib import Path; signal.signal(signal.SIGINT, signal.SIG_DFL); Path(" + repr(str(ready)) + ").touch(); time.sleep(.4); print('survived')"
        parent = "\n".join([
            'import signal,subprocess',
            'import suggest_attrs as s',
            'signal.signal(signal.SIGINT, lambda *_: None)',
            'real_run = subprocess.run',
            'def stub(command, **kwargs):',
            '    return real_run([' + repr(sys.executable) + ', "-c", ' + repr(child) + '], **kwargs)',
            's.subprocess.run = stub',
            'print(s.claude_runner("haiku", ' + repr('Crops:\n' + json.dumps([{'path': str(root / 'crop.jpg')}])) + ', ' + repr(str(root)) + '))',
        ])
        proc = subprocess.Popen([sys.executable, '-c', parent], cwd=Path(labeller.__file__).parent,
                                start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert ready.exists(), 'stub child did not start'
            os.killpg(proc.pid, signal.SIGINT)
            stdout, stderr = proc.communicate(timeout=5)
            assert proc.returncode == 0 and 'survived' in stdout, 'terminal Ctrl-C must leave paid model process running: ' + stderr
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate(timeout=5)


def main():
    review_terminal_process_group()
    review_empty_values_warning()
    review_signal_seams()
    review_cli_isolation()
    review_current_batch_interrupt()
    review_interrupt()
    review_parser()
    review_malformed()
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
