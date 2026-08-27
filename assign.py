#!/usr/bin/env python3
"""Give a curator a class to hunt for, and put it in front of them.

  python assign.py <handle> <class|CATEGORY> <min>   assign, upload, refresh
  ...where a single letter is a toll category: E covers e-heavy and e-plant
  python assign.py --clear <handle>         drop the assignment, same round-trip
  python assign.py --list                   local table + each curator's live progress
  python assign.py                          usage + self-check

dataset/assignments.yaml is read per request, but the curation instance reads its OWN
copy on its own volume: an edit only reaches the team once it is in the bucket AND the
instance has pulled it down, which is the refresh lever's whole job.

CURATION_URL / CURATION_TOKEN override where that lever is pressed and as whom.
"""

import os
import sys
from pathlib import Path

import yaml

import dataset_sync

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset"
FILE = DATASET / "assignments.yaml"
URL = os.environ.get("CURATION_URL", "https://curation-production.up.railway.app").rstrip("/")
TIMEOUT = 60          # the refresh endpoint answers immediately; it syncs on its own thread


def classes():
    """The operator's taxonomy, positional ids — the same list the Label tab offers."""
    try:
        return [c.strip() for c in (DATASET / "classes.txt").read_text().splitlines() if c.strip()]
    except OSError:
        return []


def target_classes(cls, names):
    """Same rule as app.target_classes: each comma-separated term is a toll category
    letter (every `<letter>-*` class), one exact class name, or "all"."""
    out = []
    for t in (p.strip().lower() for p in str(cls or "").split(",") if p.strip()):
        if t == "all":
            out += [c for c in names if "-" in c]
        elif len(t) == 1:
            out += [c for c in names if c.lower().startswith(t + "-")]
        else:
            out += [c for c in names if c.lower() == t]
    return [c for c in names if c in set(out)]


def curators():
    """{handle: token}. A missing or broken roster is empty, not fatal: assignments are
    the operator's business, and the instance is the one that checks tokens."""
    try:
        v = yaml.safe_load((DATASET / "curators.yaml").read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return {str(k): str(t) for k, t in v.items() if t} if isinstance(v, dict) else {}


def load(path=FILE):
    """Same tolerance as app.assignments(): anything unreadable means nobody is assigned."""
    try:
        v = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return {k: a for k, a in v.items() if isinstance(a, dict)} if isinstance(v, dict) else {}


def save(entries, path=FILE):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(entries, default_flow_style=False))
    return entries


def assign(who, cls, least, path=FILE, names=None):
    names = names if names is not None else classes()
    covers = target_classes(cls, names)
    if not covers:
        cats = sorted({c[0].upper() for c in names if c[1:2] == "-"})
        sys.exit(f"no class or category {cls!r} — pick one of: "
                 f"{', '.join(cats + names) or '(no classes.txt)'}")
    cls = cls.upper() if len(cls) == 1 else cls
    roster = curators()
    if not roster:
        print(f"! no roster at {DATASET / 'curators.yaml'} — assigning {who} anyway")
    elif who not in roster:
        print(f"! {who} is not in curators.yaml — they cannot log in until they are")
    entries = load(path)
    entries[who] = {"class": cls, "min": least}
    print(f"{who} -> {cls} (min {least})"
          + (f" = {', '.join(covers)}" if covers != [cls] else ""))
    return save(entries, path)


def clear(who, path=FILE):
    entries = load(path)
    if who not in entries:
        sys.exit(f"{who} has no assignment in {path}")
    del entries[who]
    print(f"{who} -> unassigned")
    return save(entries, path)


def admin_token(roster=None):
    """The token the refresh lever needs. CURATION_TOKEN wins; otherwise the first handle
    on the roster that the instance's REVIEWERS list also names — refresh is reviewer-only."""
    reviewers = [w.strip() for w in os.environ.get("REVIEWERS", "").split(",") if w.strip()]
    roster = curators() if roster is None else roster
    return os.environ.get("CURATION_TOKEN") or next(
        (t for h, t in roster.items() if h in reviewers), "")


def publish(path=FILE):
    """Upload this one file, unconditionally — dataset_sync.push skips same-name-same-size,
    which is exactly the shape of a class swap that happens to be the same length."""
    o = dataset_sync.creds()
    key = dataset_sync.PREFIX + path.name
    dataset_sync.client(o).upload_file(str(path), o["bucket"], key)
    print(f"uploaded -> {key} on {o['bucket']}")


def refresh():
    """Make the instance pull what we just pushed. Skipped, loudly, when no token resolves:
    the change is in the bucket either way, it just waits for the next press."""
    import requests

    tok = admin_token()
    if not tok:
        print(f"! no admin token — set CURATION_TOKEN, or REVIEWERS to a handle in "
              f"curators.yaml, or press it by hand: curl -X POST {URL}/api/dataset/refresh "
              f"-H 'X-Curator-Token: <reviewer token>'")
        return
    try:
        r = requests.post(f"{URL}/api/dataset/refresh", headers={"X-Curator-Token": tok},
                          timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"! refresh could not reach {URL}: {e} — the file is in the bucket, press it later")
        return
    print(f"refresh {r.status_code}: {r.text[:200]}")


def progress():
    """{handle: row} from the live instance, or {} when it cannot be reached. Reads need
    any roster token, not a reviewer's: the instance gates every dataset endpoint."""
    import requests

    tok = admin_token() or next(iter(curators().values()), "")
    if not tok:
        return {}
    try:
        r = requests.get(f"{URL}/api/dataset/progress", headers={"X-Curator-Token": tok},
                         timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("by_who") or {}
    except (requests.RequestException, ValueError) as e:
        print(f"! live progress unavailable ({e}) — showing the local file only")
        return {}


def show():
    entries, live = load(), progress()
    if not entries:
        print(f"no assignments in {FILE}")
    print(f"{'curator':<14}{'class':<14}{'min':>5}{'done':>6}{'approvals':>11}")
    for who in sorted(set(entries) | set(live)):
        a, row = entries.get(who) or {}, live.get(who) or {}
        # done comes from the instance's own copy of the file: a dash means it has not
        # pulled this assignment yet, or the curator has not hit one.
        done = (row.get("assignment") or {}).get("done", "-")
        print(f"{who:<14}{str(a.get('class') or '-'):<14}{str(a.get('min', '-')):>5}"
              f"{str(done):>6}{str(row.get('approve', '-')):>11}")


def selfcheck():
    import tempfile
    from unittest.mock import patch

    tmp = Path(tempfile.mkdtemp()) / "assignments.yaml"     # never the real dataset/
    names = ["e-heavy", "b-light"]

    try:
        assign("curator01", "lorry", 50, tmp, names)
        raise AssertionError("an unknown class must be rejected")
    except SystemExit as e:
        assert "e-heavy" in str(e), e                       # and the message says what is valid
    assert not tmp.exists(), "a rejected assignment must not write anything"

    assert assign("curator01", "e-heavy", 50, tmp, names) == {
        "curator01": {"class": "e-heavy", "min": 50}}
    assert load(tmp) == {"curator01": {"class": "e-heavy", "min": 50}}, load(tmp)

    assign("curator02", "b-light", 10, tmp, names)
    assert assign("curator01", "b-light", 200, tmp, names)["curator01"] == {
        "class": "b-light", "min": 200}, "a second assign replaces, never appends"
    assert set(load(tmp)) == {"curator01", "curator02"}

    # A category letter is stored upper-case and covers every class under it.
    assert assign("curator01", "e", 5, tmp, names)["curator01"] == {"class": "E", "min": 5}
    assert target_classes("E", names + ["e-plant", "wheel"]) == ["e-heavy", "e-plant"]
    assert not target_classes("z", names) and not target_classes("", names)

    assign("curator01", "b-light", 200, tmp, names)
    assert set(clear("curator01", tmp)) == {"curator02"}
    assert load(tmp) == {"curator02": {"class": "b-light", "min": 10}}, load(tmp)
    try:
        clear("curator01", tmp)
        raise AssertionError("clearing nothing must not push an unchanged file")
    except SystemExit:
        pass

    # A handle nobody can log in as is a warning, not a refusal: the roster may be next.
    assert "ghost" in assign("ghost", "e-heavy", 1, tmp, names)

    roster = {"jonah": "tok-j", "curator02": "tok-2"}
    with patch.dict(os.environ, {"CURATION_TOKEN": "", "REVIEWERS": ""}):
        assert not admin_token(roster), "no reviewer named, no refresh"
        with patch.dict(os.environ, {"REVIEWERS": "curator02,jonah"}):
            assert admin_token(roster) == "tok-j", "roster order decides, not REVIEWERS order"
        with patch.dict(os.environ, {"CURATION_TOKEN": "env-tok", "REVIEWERS": "jonah"}):
            assert admin_token(roster) == "env-tok", "CURATION_TOKEN wins"

    print("assign self-check ok: unknown target rejected, category letters cover their "
          "classes, entries replace and clear, unknown handle warns, admin token resolves")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__.strip(), "\n")
        selfcheck()
    elif args[0] == "--list":
        show()
    elif args[0] == "--clear":
        if len(args) != 2:
            sys.exit("usage: assign.py --clear <handle>")
        clear(args[1])
        publish()
        refresh()
    elif len(args) == 3 and not args[0].startswith("-"):
        if not args[2].isdigit():
            sys.exit(f"min must be a whole number, not {args[2]!r}")
        assign(args[0], args[1], int(args[2]))
        publish()
        refresh()
    else:
        sys.exit(__doc__)
