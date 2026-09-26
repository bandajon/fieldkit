"""Small, fail-closed retention policy for curation sample transfers."""
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
POLICY_NAME = "retention-policy.json"
STAMP = re.compile(r"^(\d{8})-(\d{6})$")
TREES = {"pending", "holding", "approved", "archive"}
PARTS = {"images": ".jpg", "labels": ".txt", "attrs": ".json", "suggest": ".json"}
EXEMPT = ("audit.jsonl", "scores.jsonl", "gold-served.jsonl", "classes.txt", "attributes.yaml",
          "curators.yaml", "trusted.yaml", "assignments.yaml", "reference.txt", "trained.txt",
          "external.json", "loop-state", "loop.lock")
_CACHE = {}
_CACHE_LOCK = threading.Lock()
DATASET_LOCK = threading.RLock()


def cached_policy(root):
    path = Path(root).resolve() / POLICY_NAME
    if path.is_symlink(): raise ValueError("retention policy is symlink")
    try: sig = (str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size)
    except FileNotFoundError: return None
    except OSError: raise ValueError("retention policy unavailable") from None
    with _CACHE_LOCK:
        if sig in _CACHE: return _CACHE[sig]
        try: policy = validate_policy(json.loads(path.read_text(encoding="utf-8")))
        except Exception: raise ValueError("invalid retention policy") from None
        _CACHE.clear(); _CACHE[sig] = policy
        return policy


def _dt(value):
    if not isinstance(value, str): raise ValueError("invalid retention datetime")
    try:
        out = datetime.fromisoformat(value)
    except ValueError: raise ValueError("invalid retention datetime") from None
    if out.tzinfo is None: raise ValueError("retention datetime needs UTC offset")
    return out.astimezone(UTC)


def _iso(value):
    return _dt(value).isoformat()


def validate_policy(policy):
    if not isinstance(policy, dict) or set(policy) != {"version", "days", "not_before", "expires_before", "updated_at", "unknown_admitted_at"}:
        raise ValueError("invalid retention policy")
    if isinstance(policy["version"], bool) or policy["version"] != 1 or isinstance(policy["days"], bool) or policy["days"] != 7 or not isinstance(policy["unknown_admitted_at"], dict):
        raise ValueError("invalid retention policy")
    nb, cutoff, updated = map(_dt, (policy["not_before"], policy["expires_before"], policy["updated_at"]))
    if cutoff < nb or updated < nb or cutoff > updated: raise ValueError("retention policy regresses")
    for sid, admitted in policy["unknown_admitted_at"].items():
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", sid): raise ValueError("invalid unknown sample")
        if _dt(admitted) > updated: raise ValueError("future unknown admission")
    return policy


def advance_policy(previous, now, unknown_ids=()):
    now = _dt(now.isoformat() if isinstance(now, datetime) else now)
    old = validate_policy(previous) if previous is not None else None
    nb = _dt(old["not_before"]) if old else datetime(2026, 9, 2, 22, tzinfo=UTC)
    authority = max(now, _dt(old["updated_at"]) if old else now)
    cutoff = max([nb, authority - timedelta(days=7)] + ([_dt(old["expires_before"])] if old else []))
    admitted = dict(old["unknown_admitted_at"] if old else {})
    for sid in unknown_ids:
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", sid): raise ValueError("invalid unknown sample")
        admitted.setdefault(sid, authority.isoformat())
    return validate_policy({"version": 1, "days": 7, "not_before": nb.isoformat(),
                            "expires_before": cutoff.isoformat(), "updated_at": authority.isoformat(),
                            "unknown_admitted_at": admitted})


def sample_id(rel):
    """Return a curation SID; None means exempt/permanent, malformed raises."""
    if not isinstance(rel, str) or not rel or rel.startswith(("/", "\\")) or "\\" in rel:
        raise ValueError("invalid dataset path")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts): raise ValueError("invalid dataset path")
    if parts[0] == "classifier-crops": return None
    if parts[0] in {"audit.jsonl", "scores.jsonl", "gold-served.jsonl", "classes.txt", "attributes.yaml", "curators.yaml", "trusted.yaml", "assignments.yaml", "reference.txt", "external.json", "trained.txt", "supervisors.yaml", "retention-policy.json", "loop-state", "loop.lock"} or parts[-1] == ".DS_Store":
        return None
    if len(parts) == 3 and parts[0] in {"pending", "holding", "approved"} and parts[1] in PARTS and re.fullmatch(r"[A-Za-z0-9_-]{1,200}" + re.escape(PARTS[parts[1]]), parts[2]):
        return parts[2].rsplit(".", 1)[0]
    if len(parts) >= 3 and parts[0] == "gold" and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", parts[1]):
        return parts[1]
    if len(parts) == 4 and parts[0] == "archive" and parts[2] in PARTS and re.fullmatch(r"[A-Za-z0-9_-]{1,200}" + re.escape(PARTS[parts[2]]), parts[3]):
        return parts[3].rsplit(".", 1)[0]
    if parts[0] in {"pending", "holding", "approved", "gold", "archive"}:
        raise ValueError("invalid curation artifact path")
    return None


def _known(sid):
    m = re.search(r"(?:^|-)(\d{8})-(\d{6})$", sid)
    if not m: return None
    try: return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError: return None


def expired(sid, policy, now=None):
    """Policy must already be validated; performs O(1) expiry lookup."""
    authority = _dt(now or policy["updated_at"])
    captured = _known(sid)
    if captured is not None: return captured < _dt(policy["expires_before"])
    admitted = policy["unknown_admitted_at"].get(sid)
    return admitted is not None and _dt(admitted) + timedelta(days=7) <= authority
