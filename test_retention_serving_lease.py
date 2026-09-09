import ast
import functools
import subprocess
import sys
import tempfile
import threading
import time
import types
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import retention_maintenance as rm
import dataset_sync as real_dataset_sync


def _child(root):
    code = """import sys, time
from retention_maintenance import serving_lease
with serving_lease(sys.argv[1]) as ok:
    print(ok, flush=True)
    time.sleep(3)
"""
    return subprocess.Popen([sys.executable, "-c", code, str(root)], stdout=subprocess.PIPE, text=True)


def main():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        holder = _child(root)
        assert holder.stdout.readline().strip() == "True"
        calls = []
        fake_sync = types.SimpleNamespace(
            creds=lambda: calls.append("creds") or {"bucket": "b"},
            client=lambda _: object(),
        )
        sys.modules["dataset_sync"] = fake_sync
        try:
            rm.main(["once", "--dataset", str(root), "--quiesced"])
        except SystemExit as exc:
            assert str(exc) == "busy"
        else:
            raise AssertionError("CLI once did not report a busy serving lease")
        assert calls == []
        with rm.serving_lease(root) as locked:
            assert not locked
        with rm.dataset_lock(root) as loop_locked:
            assert loop_locked
        holder.terminate(); holder.wait(timeout=5)
        with rm.serving_lease(root) as released:
            assert released
        original_once = rm.maintenance_once
        rm.maintenance_once = lambda root, cl, bucket: {"ok": True}
        try:
            rm.main(["once", "--dataset", str(root), "--quiesced"])
        finally:
            rm.maintenance_once = original_once
        assert calls == ["creds"]
        sys.modules["dataset_sync"] = real_dataset_sync

    import dataset_retention as policy
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        current = policy.advance_policy(None, "2026-09-09T00:00:00+00:00")
        policy_raw = (__import__("json").dumps(current) + "\n").encode()
        class Client:
            def get_paginator(self, _): return self
            def paginate(self, **_):
                yield {"Contents": [{"Key": "curation/retention-policy.json", "Size": len(policy_raw)},
                                     {"Key": "curation/classifier-crops/a/manifest.json", "Size": 2}]}
            def get_object(self, **_): return {"Body": BytesIO(policy_raw)}
            def download_file(self, bucket, key, dest): Path(dest).write_bytes(b"{}")
        result = rm.maintenance_once(root, Client(), "b")
        assert result["disabled"] is False
        assert (root / "classifier-crops/a/manifest.json").read_bytes() == b"{}"

    tree = ast.parse(Path(__file__).with_name("app.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "dataset_mutation")
    ns = {"functools": functools, "HTTPException": type("HTTPException", (Exception,), {}),
          "DATASET_LOCK": threading.RLock()}
    exec(compile(ast.Module([node], type_ignores=[]), "app.py", "exec"), ns)
    @ns["dataset_mutation"]
    def nested():
        with ns["DATASET_LOCK"]:
            return "ok"
    assert nested() == "ok"
    ns["DATASET_LOCK"].acquire()
    result = []
    def call():
        try:
            nested()
        except Exception as exc:
            result.append(exc)
    t = threading.Thread(target=call)
    t.start(); t.join()
    assert len(result) == 1 and isinstance(result[0], ns["HTTPException"])
    ns["DATASET_LOCK"].release()
    print("ok — serving lease, independent loop lock, and fail-fast dataset mutation")


if __name__ == "__main__":
    main()
