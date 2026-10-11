#!/usr/bin/env python3
"""Pre-fill pending suggestions locally, or opt in to Claude colour/make labelling.

    python suggest_attrs.py          # ask gemma about every un-suggested heavy box
    python suggest_attrs.py check    # count what would be asked, no requests
    python suggest_attrs.py claude [--limit N]

Pending frames get colour/make from curators in the Label tab (heads come from attributes.yaml).
Claude writes immutable dataset/ai-attrs/<UTC run id>.jsonl records.
"""

import base64
import argparse
import io
import json
import math
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import islice

# Background launches (nohup/&) have SIGINT ignored at the OS level, so Ctrl-C-grace
# must also answer SIGTERM: plain `kill` finishes the current sample and stops.
def _term(*_):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _term)

import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
PENDING = DATASET / "pending"

OLLAMA = "http://localhost:11434"    # local only: crops carry plates
MODEL = "gemma4:12b"
TARGET_CLASSES = ("d-medium", "e-heavy", "f-abnormal", "e-plant", "b-light")
PAD = 0.10                # same context padding the classifier trains on
CROP_MAX = 896            # axle counting needs pixels; 224 is the classifier's problem
TIMEOUT_S = 300           # one crop, cold model load included
PROGRESS_EVERY = 5    # ~40 s/crop on this Mac: silence for 25 crops reads as a hang


def vocab():
    f = DATASET / "attributes.yaml"
    if not f.is_file():
        sys.exit(f"no {f}")
    cfg = yaml.safe_load(f.read_text()) or {}
    heads = {k: [str(x) for x in v] for k, v in cfg.items() if isinstance(v, list)}
    return heads, cfg.get("constraints") or {}, cfg.get("implies") or {}


def allowed(cls, heads, constraints):
    """Head -> values askable for this class. Constraint list wins; an empty list
    means the head does not apply and is not asked at all."""
    c = constraints.get(cls) or {}
    out = {}
    for h, vals in heads.items():
        a = [str(x) for x in c[h]] if h in c else vals
        if a:
            out[h] = a
    return out


def crop_jpeg(img, box):
    """YOLO cx cy w h -> padded, clamped crop as JPEG bytes, or None if degenerate."""
    W, H = img.size
    cx, cy, w, h = (float(v) for v in box)
    w, h = w * (1 + 2 * PAD), h * (1 + 2 * PAD)
    x1, y1 = max(0, (cx - w / 2) * W), max(0, (cy - h / 2) * H)
    x2, y2 = min(W, (cx + w / 2) * W), min(H, (cy + h / 2) * H)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return None
    crop = img.crop((x1, y1, x2, y2))
    crop.thumbnail((CROP_MAX, CROP_MAX))
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def ask(cls, opts, jpeg):
    """One call, all heads. -> {head: value} straight from the model (unvalidated)."""
    lines = "\n".join(f"- {h}: " + ", ".join(v) for h, v in opts.items())
    prompt = (
        f"You are labelling ONE vehicle from a Zambian toll-gate camera. A detector has "
        f"already classified it as '{cls}'; classify the largest, most central vehicle in "
        f"the crop and ignore anything behind it.\n"
        f"Answer each attribute with exactly one of its listed values, or \"unknown\" if the "
        f"image does not show it:\n{lines}\n"
        "axles counts axles on the ground, tractor plus trailers together. axle-config groups "
        "them front to back (1+2 = single steer, tandem drive). trailers counts towed units.\n"
        "Answer with a single JSON object mapping each attribute to its value.")
    schema = {"type": "object",
              "properties": {h: {"type": "string", "enum": v + ["unknown"]}
                             for h, v in opts.items()},
              "required": list(opts)}
    body = json.dumps({
        "model": MODEL,
        # image before text is the documented order for gemma vision
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(jpeg).decode()]}],
        "stream": False, "format": schema, "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(json.load(r)["message"]["content"])


def clean(answer, opts, implies):
    """Keep only in-vocabulary values. Where axle-config and axles disagree, the
    config wins: counting axles off a photo is the guess, reading the grouping is
    the observation."""
    out = {h: v for h, v in (answer or {}).items()
           if h in opts and isinstance(v, str) and v in opts[h]}
    cfg = out.get("axle-config")
    implied = ((implies.get("axle-config") or {}).get(cfg) or {}).get("axles")
    if implied and out.get("axles") and out["axles"] != implied:
        out.pop("axles")
    return out


def load_json(path):
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def todo(classes):
    """-> [(stem, image path, {box index: (class name, box)})] for un-suggested boxes."""
    work = []
    for lbl in sorted((PENDING / "labels").glob("*.txt")):
        img = PENDING / "images" / f"{lbl.stem}.jpg"
        if not img.is_file():
            continue
        done = set(load_json(PENDING / "attrs" / f"{lbl.stem}.json")) | \
            set(load_json(PENDING / "suggest" / f"{lbl.stem}.json"))
        boxes = {}
        for i, line in enumerate(lbl.read_text().splitlines()):
            f = line.split()
            if len(f) != 5 or str(i) in done or not f[0].isdigit():
                continue
            cls = classes[int(f[0])] if int(f[0]) < len(classes) else ""
            if cls in TARGET_CLASSES:
                boxes[i] = (cls, f[1:])
        if boxes:
            work.append((lbl.stem, img, boxes))
    return work


def claude_runner(model, prompt, cwd):
    return subprocess.run(
        ["claude", "-p", "--model", model, "--allowedTools", "Read",
         "--setting-sources", "project", "--strict-mcp-config"],
        input=prompt, text=True, capture_output=True, check=True,
        timeout=TIMEOUT_S,
        cwd=cwd,
        **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
           else {"start_new_session": True}),
    ).stdout


def claude_answers(stdout, items, implies):
    """Extract the JSON list from CLI prose; bind answers only to supplied IDs."""
    decoder = json.JSONDecoder()
    for start, char in enumerate(stdout):
        if char != "[":
            continue
        try:
            rows, _ = decoder.raw_decode(stdout[start:])
        except ValueError:
            continue
        if not isinstance(rows, list) or not any(isinstance(row, dict) and "id" in row for row in rows):
            continue
        opts = {item["id"]: item["values"] for item in items}
        answers, seen = {}, set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("id"))
            if key not in opts:
                continue
            if key in seen:
                answers.pop(key, None)
                continue
            seen.add(key)
            answers[key] = clean(row, opts[key], implies)
        return answers
    raise ValueError("Claude returned no JSON list")


def ai_entries():
    entries = {}
    for path in sorted((DATASET / "ai-attrs").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                if (isinstance(row, dict) and isinstance(row.get("sid"), str)
                        and type(row.get("box")) is int and row["box"] >= 0):
                    entries[(row["sid"], row["box"])] = row
            except ValueError:
                continue
    return entries


def claude_work(classes, heads, constraints, limit):
    import dataset_retention
    tree = DATASET / "approved"
    with dataset_retention.DATASET_LOCK:
        entries = ai_entries()
    count = 0
    for label in sorted((tree / "labels").glob("*.txt")):
        if limit is not None and count >= limit:
            return
        stem = label.stem
        with dataset_retention.DATASET_LOCK:
            policy = dataset_retention.cached_policy(DATASET)
            if policy and dataset_retention.expired(stem, policy):
                continue
            human = load_json(tree / "attrs" / f"{stem}.json")
            try:
                lines = label.read_text().splitlines()
                with Image.open(tree / "images" / f"{stem}.jpg") as source:
                    img = source.convert("RGB")
            except OSError:
                continue
        for i, line in enumerate(lines):
            if limit is not None and count >= limit:
                return
            fields = line.split()
            if len(fields) != 5 or not fields[0].isdigit() or int(fields[0]) >= len(classes):
                continue
            cls = classes[int(fields[0])]
            if cls in ("a-motorcycle", "e-plant"):
                continue
            try:
                box = [float(v) for v in fields[1:]]
            except ValueError:
                continue
            if not all(math.isfinite(v) for v in box) or box[2] * img.width < 90 or box[3] * img.height < 60:
                continue
            key = str(i)
            labelled = human.get(key, {})
            if all(h in labelled for h in ("colour", "make")):
                continue
            if entries.get((stem, i), {}).get("bbox") == box:
                continue
            opts = {h: v for h, v in allowed(cls, heads, constraints).items()
                    if h in ("colour", "make") and h not in labelled}
            if not opts:
                continue
            jpeg = crop_jpeg(img, box)
            if jpeg is not None:
                count += 1
                yield {"sid": stem, "box": i, "bbox": box, "line": line,
                       "class": cls, "values": opts}, jpeg


def run_claude(limit=None, runner=claude_runner):
    # Opt-in: Claude crops leave the machine; the Ollama mode stays local-only.
    import dataset_retention
    heads, constraints, implies = vocab()
    classes = [c.strip() for c in (DATASET / "classes.txt").read_text().splitlines() if c.strip()]
    work = claude_work(classes, heads, constraints, limit)
    began, done, skipped = time.monotonic(), 0, 0
    stopping = False
    print("Claude approved: colour + make, batches of 10" + (f", limit {limit}" if limit is not None else ""))
    def stop_after_batch(*_):
        nonlocal stopping
        stopping = True

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, stop_after_batch)
        with ThreadPoolExecutor(max_workers=4) as pool:
            while not stopping and (batch := list(islice(work, 10))):
                rows = []
                with tempfile.TemporaryDirectory(prefix="fieldkit-claude-") as tmp:
                    items, metadata = [], {}
                    with dataset_retention.DATASET_LOCK:
                        policy = dataset_retention.cached_policy(DATASET)
                        for n, (item, jpeg) in enumerate(batch):
                            if policy and dataset_retention.expired(item["sid"], policy):
                                skipped += 1
                                continue
                            key = str(n)
                            path = Path(tmp) / f"{n}.jpg"
                            path.write_bytes(jpeg)
                            items.append({"id": key, "path": str(path.resolve()),
                                          "class": item["class"], "values": item["values"]})
                            metadata[key] = item
                    if not items:
                        continue
                    prompt = (
                        "Label the largest central vehicle in each crop from a Zambian toll-gate camera. "
                        "Use only the supplied vocabulary for each head. Colour is the CAB colour for "
                        "trucks/tractors, main body colour otherwise. For buses, make is the visible "
                        "body builder (Marcopolo, Higer, Yutong, Irizar, Zhongtong), else the chassis make. "
                        "Use unknown when it genuinely cannot be read; do not guess. "
                        "Return a JSON list of objects with id, colour and make. Use the exact supplied id.\n"
                        "Crops:\n" + json.dumps(items)
                    )
                    futures = {m: pool.submit(runner, m, prompt, tmp) for m in ("haiku", "sonnet")}
                    answers, failures = {}, []
                    for model, future in futures.items():
                        try:
                            while True:
                                try:
                                    output = future.result()
                                    break
                                except KeyboardInterrupt:
                                    stopping = True
                                    if future.done() and isinstance(future.exception(), KeyboardInterrupt):
                                        output = ""
                                        break
                            answers[model] = claude_answers(output, items, implies)
                        except Exception as exc:
                            failures.append(f"{model}: {exc}")
                            answers[model] = {}
                    if failures or not any(attrs for answer in answers.values() for attrs in answer.values()):
                        print("  Claude warning: " + ("; ".join(failures) or "no usable answer"), flush=True)
                    with dataset_retention.DATASET_LOCK:
                        policy = dataset_retention.cached_policy(DATASET)
                        entries = ai_entries()
                        for key, item in metadata.items():
                            stem, index = item["sid"], item["box"]
                            tree = DATASET / "approved"
                            if policy and dataset_retention.expired(stem, policy):
                                skipped += 1
                                continue
                            label = tree / "labels" / f"{stem}.txt"
                            if not label.exists() or label.read_text().splitlines()[index:index + 1] != [item["line"]]:
                                skipped += 1
                                continue
                            attrs = answers["sonnet"].get(key, {})
                            attrs = {h: v for h, v in attrs.items()
                                     if answers["haiku"].get(key, {}).get(h) == v}
                            human = load_json(tree / "attrs" / f"{stem}.json").get(str(index), {})
                            attrs = {h: v for h, v in attrs.items() if h not in human}
                            if not attrs or entries.get((stem, index), {}).get("bbox") == item["bbox"]:
                                skipped += 1
                                continue
                            row = {"sid": stem, "box": index, "bbox": item["bbox"], "attrs": attrs,
                                   "models": ["haiku", "sonnet"], "at": datetime.now(timezone.utc).isoformat()}
                            rows.append(row)
                            done += 1
                        if rows:
                            directory = DATASET / "ai-attrs"
                            directory.mkdir(parents=True, exist_ok=True)
                            run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                            with tempfile.NamedTemporaryFile(mode="w", dir=DATASET, prefix=f".{run_id}-",
                                                             suffix=".tmp", delete=False) as ledger:
                                tmp = Path(ledger.name)
                                try:
                                    ledger.writelines(json.dumps(row) + "\n" for row in rows)
                                except BaseException:
                                    tmp.unlink(missing_ok=True)
                                    raise
                            try:
                                dest = directory / (tmp.stem[1:] + ".jsonl")
                                if dest.exists():
                                    raise FileExistsError(dest)
                                os.replace(tmp, dest)
                            finally:
                                tmp.unlink(missing_ok=True)
                rate = (done + skipped) / max(time.monotonic() - began, 1e-6)
                print(f"  {done} done, {skipped} skipped, {rate * 60:.1f}/min", flush=True)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(f"{done} suggested, {skipped} skipped in {(time.monotonic() - began) / 60:.1f} min")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "claude":
        parser = argparse.ArgumentParser(description="Opt-in Claude colour/make labeller")
        parser.add_argument("--limit", type=int)
        args = parser.parse_args(sys.argv[2:])
        if args.limit is not None and args.limit < 0:
            parser.error("--limit must be nonnegative")
        if shutil.which("claude") is None:
            parser.exit(1, "Claude executable not found on PATH; install it before running Claude labelling.\n")
        run_claude(args.limit)
        return
    heads, constraints, implies = vocab()
    classes = [c.strip() for c in (DATASET / "classes.txt").read_text().splitlines() if c.strip()]
    work = todo(classes)
    crops = sum(len(b) for _, _, b in work)
    print(f"{len(work)} samples, {crops} boxes to suggest "
          f"({', '.join(TARGET_CLASSES)})")
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        by_class = {}
        for _, _, boxes in work:
            for cls, _ in boxes.values():
                by_class[cls] = by_class.get(cls, 0) + 1
        for cls, n in sorted(by_class.items(), key=lambda kv: -kv[1]):
            print(f"  {cls:<12} {n:>6}   heads: " + ", ".join(allowed(cls, heads, constraints)))
        return
    if not crops:
        return
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=5).close()
    except (urllib.error.URLError, OSError):
        sys.exit(f"no ollama at {OLLAMA} — start it (ollama serve) and pull {MODEL}")

    began, done, skipped, stopping = time.monotonic(), 0, 0, False
    import dataset_retention
    for stem, img_path, boxes in work:
        got = {}
        try:
            with dataset_retention.DATASET_LOCK:
                policy = dataset_retention.cached_policy(DATASET)
                if policy and dataset_retention.expired(stem, policy):
                    continue
            img = Image.open(img_path).convert("RGB")
            for i, (cls, box) in boxes.items():
                jpeg = crop_jpeg(img, box)
                if jpeg is None:
                    skipped += 1
                    continue
                opts = allowed(cls, heads, constraints)
                try:
                    v = clean(ask(cls, opts, jpeg), opts, implies)
                except Exception:      # timeout, refused, garbage JSON: one crop, not the batch
                    skipped += 1
                    continue
                done += 1
                if v:
                    got[str(i)] = v
                if (done + skipped) % PROGRESS_EVERY == 0:
                    rate = done / max(time.monotonic() - began, 1e-6)
                    print(f"  {done} done, {skipped} skipped, {rate * 60:.1f}/min", flush=True)
        except KeyboardInterrupt:
            stopping = True            # finish this sample, then stop: written files stand
        except OSError:
            skipped += len(boxes)
        if got:
            out = PENDING / "suggest" / f"{stem}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            with dataset_retention.DATASET_LOCK:
                policy = dataset_retention.cached_policy(DATASET)
                if not policy or not dataset_retention.expired(stem, policy):
                    out.write_text(json.dumps({**load_json(out), **got}))
        if stopping:
            break
    print(f"{done} suggested, {skipped} skipped in {(time.monotonic() - began) / 60:.1f} min")


if __name__ == "__main__":
    main()
