"""Stage 1 paired-review catalog and frame extraction from the recording bucket."""
import base64, hashlib, json, math, os, re, subprocess, tempfile, threading, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

CAT = timezone(timedelta(hours=2))
KEY = re.compile(r"\A([^/\\]+)/([^/\\]+)/([0-9]{8})-([0-9]{6})\.mkv\Z")
CAM = re.compile(r"\A[A-Za-z0-9_.-]{1,80}\Z")
DATE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
PTS = re.compile(rb"showinfo.*?\bn:\s*0\b.*?pts_time:\s*([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
MAX_SOURCE, MAX_JPEG = 500 * 1024 * 1024, 5 * 1024 * 1024
MAX_JSON = 64 * 1024
PAIRED_PREFIX = "curation-paired/v1/"
EXAMPLE_ID = re.compile(r"\A[0-9a-f]{32}\Z")
CALIBRATION_ID = re.compile(r"\A[0-9a-f]{64}\Z")
VISIBILITIES = {"full", "partial", "occluded"}
VERDICTS = {"same", "different", "uncertain"}
NON_RECORDING_ROOTS = {"classifier-crops-history", "curation", "fieldkit-events", "models"}
PAIR_LOCK = threading.RLock()
class RecordingMissing(LookupError): pass
class RecordingUnavailable(LookupError): pass

def _finite(v): return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)

def _key(value):
    m = KEY.fullmatch(value) if isinstance(value, str) else None
    if not m: raise ValueError("invalid recording key")
    site, cam, day, clock = m.groups(); stamp = f"{day}-{clock}"
    if site in (".", "..") or cam in (".", "..") or not CAM.fullmatch(site) or not CAM.fullmatch(cam): raise ValueError("invalid recording key")
    try: datetime.strptime(stamp, "%Y%m%d-%H%M%S")
    except ValueError: raise ValueError("invalid recording key") from None
    return site, cam, stamp

def _epoch(stamp): return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=CAT).timestamp()

def _check_filter(site, date):
    if site is not None and not CAM.fullmatch(site): raise ValueError("invalid site")
    if site in NON_RECORDING_ROOTS: raise ValueError("invalid site")
    if date is not None and not DATE.fullmatch(date): raise ValueError("invalid date")
    if date:
        try: datetime.strptime(date, "%Y-%m-%d")
        except ValueError: raise ValueError("invalid date") from None

def _prefixes(client, bucket, prefix="", delimiter="/"):
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter=delimiter):
        yield from (x["Prefix"] for x in page.get("CommonPrefixes", []))

def _camera_prefixes(client, bucket, site):
    return _prefixes(client, bucket, site + "/")

def _direct_objects(client, bucket, prefix):
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        yield from page.get("Contents", [])

def catalog(client, bucket, site=None, date=None):
    _check_filter(site, date)
    sites = [site] if site else [p.rstrip("/") for p in _prefixes(client, bucket)
                                  if p.rstrip("/") not in NON_RECORDING_ROOTS]
    all_recordings = []
    for selected in sites:
        for camera_prefix in _camera_prefixes(client, bucket, selected):
            # ponytail: fixed three-level listing; never recurse through camera contents.
            for obj in _direct_objects(client, bucket, camera_prefix):
                key = obj.get("Key", "")
                try: s, cam, stamp = _key(key)
                except ValueError: continue
                start = _epoch(stamp); size = obj.get("Size")
                if not isinstance(size, int) or size <= 0: continue
                all_recordings.append({"key": key, "site": s, "camera_id": cam, "filename": key.rsplit("/", 1)[1], "start_ts": start, "size": size})
    out = [x for x in all_recordings if not date or datetime.fromtimestamp(x["start_ts"], CAT).strftime("%Y-%m-%d") == date]
    out.sort(key=lambda x: (x["start_ts"], x["key"]))
    return {"sites": sorted({x["site"] for x in all_recordings}), "dates": sorted({datetime.fromtimestamp(x["start_ts"], CAT).strftime("%Y-%m-%d") for x in all_recordings}), "recordings": out}

def _cache_path(cache_root, key):
    _key(key); raw_base = Path(cache_root)
    if raw_base.is_symlink(): raise RecordingUnavailable("recording cache unavailable")
    base = raw_base.resolve(); base.mkdir(parents=True, exist_ok=True)
    path = base / (hashlib.sha256(key.encode()).hexdigest() + ".mkv")
    if path.is_symlink() or not path.resolve().is_relative_to(base): raise LookupError("recording unavailable")
    return base, path

def _head(client, bucket, key):
    try:
        h = client.head_object(Bucket=bucket, Key=key); size = h.get("ContentLength"); etag = str(h.get("ETag", "")).strip('"')
    except Exception as e:
        code = getattr(getattr(e, "response", {}), "get", lambda *_: None)("Error", {}).get("Code") if hasattr(getattr(e, "response", None), "get") else ""
        if str(code) in ("404", "NoSuchKey", "NotFound"): raise RecordingMissing("recording unavailable") from None
        raise RecordingUnavailable("recording unavailable") from e
    if not isinstance(size, int) or size <= 0 or size > MAX_SOURCE: raise ValueError("recording exceeds source size limit")
    return size, etag

def _retrieve(client, bucket, key, cache_root):
    base, path = _cache_path(cache_root, key); size, etag = _head(client, bucket, key); meta = path.with_suffix(".json")
    try:
        if path.is_file() and not path.is_symlink() and meta.is_file() and not meta.is_symlink():
            if json.loads(meta.read_text()) == {"key": key, "size": size, "etag": etag} and path.stat().st_size == size:
                os.utime(path, None)
                return path, size
    except (OSError, ValueError, TypeError): pass
    tmp = None
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"]; fd, name = tempfile.mkstemp(prefix=".paired-", dir=base); tmp = Path(name); written = 0
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk: break
                written += len(chunk)
                if written > MAX_SOURCE: raise ValueError("recording exceeds source size limit")
                out.write(chunk)
            out.flush(); os.fsync(out.fileno())
        if written != size or _head(client, bucket, key) != (size, etag): raise RecordingUnavailable("recording changed during download")
        os.replace(tmp, path); tmp = None
        mt = base / (meta.name + ".tmp")
        if mt.is_symlink(): raise RecordingUnavailable("recording cache unavailable")
        mt.write_text(json.dumps({"key": key, "size": size, "etag": etag})); os.replace(mt, meta)
        return path, size
    except (LookupError, ValueError): raise
    except Exception as e: raise RecordingUnavailable("recording unavailable") from e
    finally:
        if tmp is not None:
            try: tmp.unlink()
            except OSError: pass
        try:
            if "body" in locals() and hasattr(body, "close"): body.close()
        except Exception: pass

def _trim_cache(cache_root, keep=2):
    # ponytail: fixed two-clip ceiling; use an LRU only if cache pressure matters.
    files = sorted((p for p in Path(cache_root).glob("*.mkv") if not p.is_symlink()), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for path in files[keep:]:
        try: path.unlink(); path.with_suffix(".json").unlink(missing_ok=True)
        except OSError: pass

def _probe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-f", "matroska", "-select_streams", "v:0", "-show_entries", "format=start_time,duration:stream=width,height", "-of", "json", str(path)], capture_output=True, text=True, timeout=10, check=True)
        doc = json.loads(r.stdout); fmt, stream = doc["format"], doc["streams"][0]; start, duration = float(fmt.get("start_time", 0)), float(fmt["duration"]); width, height = int(stream["width"]), int(stream["height"])
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, IndexError, json.JSONDecodeError) as e: raise RecordingUnavailable("could not inspect recording") from e
    if not (_finite(start) and _finite(duration) and duration > 0 and width > 0 and height > 0): raise RecordingUnavailable("invalid recording metadata")
    return start, duration, width, height

def _frame(path, elapsed):
    try: r = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-copyts", "-protocol_whitelist", "file", "-f", "matroska", "-ss", f"{elapsed:.6f}", "-i", str(path), "-vf", "showinfo", "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e: raise RecordingUnavailable("could not decode frame") from e
    if r.returncode or not r.stdout.startswith(b"\xff\xd8") or len(r.stdout) > MAX_JPEG: raise RecordingUnavailable("frame unavailable")
    m = PTS.search(r.stderr)
    if not m: raise RecordingUnavailable("decoded frame PTS unavailable")
    return r.stdout, float(m.group(1))

def _frames_locked(client, bucket, cache_root, source_a, source_b, ts=None, delta_s=0, mode="overlap"):
    if mode not in ("overlap", "adjacent_handoff", "occlusion"): raise ValueError("invalid transition mode")
    sa, ca, sta = _key(source_a); sb, cb, stb = _key(source_b)
    if sa != sb or sta[:8] != stb[:8]: raise ValueError("recordings must share site and day")
    if mode != "occlusion" and ca == cb: raise ValueError("transition cameras must be distinct")
    if mode == "occlusion" and ca == cb and delta_s == 0: raise ValueError("occlusion times must differ")
    if not _finite(delta_s) or abs(delta_s) > 600: raise ValueError("delta_s must be finite and within ±600 seconds")
    if ts is not None and not _finite(ts): raise ValueError("selected time must be finite")
    pa, size_a = _retrieve(client, bucket, source_a, cache_root); pb, size_b = _retrieve(client, bucket, source_b, cache_root)
    sig_a, sig_b = _stat_signature(pa), _stat_signature(pb)
    psa, da, wa, ha = _probe(pa); psb, db, wb, hb = _probe(pb); start_a, start_b, delta = _epoch(sta), _epoch(stb), float(delta_s)
    lo, hi = max(start_a, start_b - delta), min(start_a + da - psa, start_b + db - psb - delta)
    if lo >= hi: raise ValueError("recordings have no common interval")
    if ts is None: ts = lo
    if not _finite(ts) or ts < lo or ts >= hi: raise ValueError("selected time is outside the common interval")
    result = []
    for side, cam, key, path, requested, source_start, size, width, height, segment_start in (("a", ca, source_a, pa, ts, psa, size_a, wa, ha, start_a), ("b", cb, source_b, pb, ts + delta, psb, size_b, wb, hb, start_b)):
        raw, decoded = _frame(path, requested - segment_start); sha = hashlib.sha256()
        if _stat_signature(path) != (sig_a if side == "a" else sig_b): raise RecordingUnavailable("recording changed during extraction")
        with path.open("rb") as inp:
            for chunk in iter(lambda: inp.read(1024 * 1024), b""): sha.update(chunk)
        if _stat_signature(path) != (sig_a if side == "a" else sig_b): raise RecordingUnavailable("recording changed during extraction")
        result.append({"side": side, "camera_id": cam, "source_key": key, "source_filename": key.rsplit("/", 1)[1], "source_sha256": sha.hexdigest(), "source_size": size, "source_start_pts_s": source_start, "decoded_pts_s": decoded, "segment_elapsed_s": decoded - source_start, "requested_ts": requested, "corrected_ts": segment_start + decoded - source_start, "offset_s": 0.0, "offset_known": False, "width": width, "height": height, "jpeg_sha256": hashlib.sha256(raw).hexdigest(), "jpeg_data_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode()})
    return {"mode": mode, "ts": ts, "delta_s": delta, "interval": [lo, hi], "frames": result}

def _stat_signature(path):
    try:
        s = Path(path).stat()
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns
    except OSError as e: raise RecordingUnavailable("recording unavailable") from e

def frames(client, bucket, cache_root, source_a, source_b, ts=None, delta_s=0, mode="overlap"):
    # ponytail: one process-wide lock serializes two users; use per-key locks only if throughput matters.
    with PAIR_LOCK:
        try:
            return _frames_locked(client, bucket, cache_root, source_a, source_b, ts, delta_s, mode)
        finally:
            _trim_cache(cache_root)

def _provider_error(exc):
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code", "") if isinstance(response, dict) else ""
    if str(code) in ("404", "NoSuchKey", "NotFound"):
        return RecordingMissing("paired object not found")
    return RecordingUnavailable("paired storage unavailable")

def _json_bytes(doc):
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()

def _id(value, kind):
    regex = EXAMPLE_ID if kind == "example" else CALIBRATION_ID
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise ValueError(f"invalid {kind} id")
    return value

def _site_date(site, date):
    _check_filter(site, date)
    if not site or not date: raise ValueError("site and date are required")
    return site, date

def _rect(value, name="rectangle"):
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise ValueError(f"invalid {name}")
    if not all(_finite(value[k]) for k in value): raise ValueError(f"invalid {name}")
    x, y, width, height = (float(value[k]) for k in ("x", "y", "width", "height"))
    if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > 1 or y + height > 1:
        raise ValueError(f"invalid {name}")
    return {"x": x, "y": y, "width": width, "height": height}

def _calibration_doc(site, body):
    if not isinstance(body, dict) or body.get("schema_version") != 1: raise ValueError("invalid calibration schema")
    mode, cameras, title, delta, zones = body.get("mode"), body.get("camera_ids"), body.get("title"), body.get("delta_s"), body.get("zones")
    if mode not in ("overlap", "adjacent_handoff", "occlusion"): raise ValueError("invalid calibration mode")
    if not isinstance(cameras, list) or len(cameras) != 2 or not all(isinstance(c, str) and CAM.fullmatch(c) and c not in (".", "..") for c in cameras): raise ValueError("invalid calibration cameras")
    if mode != "occlusion" and cameras[0] == cameras[1]: raise ValueError("calibration cameras must be distinct")
    if not isinstance(title, str) or not title.strip() or len(title) > 256: raise ValueError("invalid calibration title")
    if not _finite(delta) or abs(delta) > 600: raise ValueError("invalid calibration delta")
    if not isinstance(zones, list) or len(zones) != 2: raise ValueError("two calibration zones are required")
    return {"schema_version": 1, "mode": mode, "camera_ids": cameras, "title": title.strip(), "delta_s": float(delta), "zones": [_rect(z, "zone") for z in zones]}

def _calibration_key(site, ident): return f"{PAIRED_PREFIX}calibrations/{site}/{ident}.json"
def _example_prefix(site, date, ident): return f"{PAIRED_PREFIX}examples/{site}/{date}/{ident}/"

def _get_object(client, bucket, key, limit):
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"]
        raw = body.read(limit + 1)
        if len(raw) > limit: raise ValueError("paired object exceeds size limit")
        return raw
    except ValueError: raise
    except Exception as exc: raise _provider_error(exc) from None
    finally:
        try:
            if "body" in locals() and hasattr(body, "close"): body.close()
        except Exception: pass

def _get_json(client, bucket, key):
    try: return json.loads(_get_object(client, bucket, key, MAX_JSON))
    except json.JSONDecodeError: raise RecordingUnavailable("paired metadata unavailable") from None

def _put(client, bucket, key, body, content_type):
    try:
        response = client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
        if response is None: raise RuntimeError("missing storage acknowledgment")
    except Exception as exc: raise _provider_error(exc) from None

def _list_keys(client, bucket, prefix):
    try:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            yield from (x.get("Key", "") for x in page.get("Contents", []))
    except Exception as exc: raise _provider_error(exc) from None

def _created(doc):
    try: return datetime.fromisoformat(doc["created_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError): raise RecordingUnavailable("paired metadata unavailable") from None

def _expired(doc, now=None):
    try:
        expires = datetime.fromisoformat(doc["expires_at"].replace("Z", "+00:00"))
        return expires <= (now or datetime.now(timezone.utc))
    except (KeyError, TypeError, ValueError): return True

def _stored_calibration(client, bucket, site, ident):
    _id(ident, "calibration")
    doc = _get_json(client, bucket, _calibration_key(site, ident))
    if not isinstance(doc, dict) or doc.get("id") != ident or doc.get("site") != site: raise RecordingUnavailable("paired calibration unavailable")
    return doc

def save_calibration(client, bucket, site, body, now=None):
    if not CAM.fullmatch(site) or site in (".", "..") or site in NON_RECORDING_ROOTS: raise ValueError("invalid site")
    payload = _calibration_doc(site, body)
    ident = hashlib.sha256(_json_bytes({"site": site, **payload})).hexdigest()
    key = _calibration_key(site, ident)
    created = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    doc = {"id": ident, "site": site, "created_at": created, **payload}
    try:
        existing = _get_json(client, bucket, key)
    except RecordingMissing: existing = None
    if existing is not None: return existing
    _put(client, bucket, key, _json_bytes(doc), "application/json")
    return doc

def list_calibrations(client, bucket, site):
    if not CAM.fullmatch(site) or site in NON_RECORDING_ROOTS: raise ValueError("invalid site")
    out = []
    for key in _list_keys(client, bucket, f"{PAIRED_PREFIX}calibrations/{site}/"):
        if not key.endswith(".json"): continue
        try: out.append(_get_json(client, bucket, key))
        except RecordingMissing: continue
    return {"calibrations": sorted((x for x in out if isinstance(x, dict) and x.get("site") == site), key=lambda x: x.get("created_at", ""), reverse=True)}

def _example_body(site, date, body):
    _site_date(site, date)
    if not isinstance(body, dict): raise ValueError("invalid example schema")
    ident = _id(body.get("calibration_id"), "calibration")
    keys = body.get("source_keys")
    if not isinstance(keys, list) or len(keys) != 2: raise ValueError("two source keys are required")
    parsed = [_key(k) for k in keys]
    if any(x[0] != site or x[2][:8] != date.replace("-", "") for x in parsed): raise ValueError("source context mismatch")
    ts = body.get("ts")
    if not _finite(ts): raise ValueError("invalid example time")
    label = body.get("journey_label")
    if not isinstance(label, str) or len(label) > 256: raise ValueError("invalid journey label")
    if body.get("verdict") not in VERDICTS: raise ValueError("invalid verdict")
    views = body.get("views")
    if not isinstance(views, list) or len(views) != 2 or {v.get("side") for v in views if isinstance(v, dict)} != {"a", "b"}: raise ValueError("two example views are required")
    clean = []
    for view in views:
        if not isinstance(view, dict) or view.get("visibility") not in VISIBILITIES or not isinstance(view.get("jpeg_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", view["jpeg_sha256"]): raise ValueError("invalid example view")
        bbox = None if view["visibility"] == "occluded" else _rect(view.get("bbox"), "bbox")
        if view["visibility"] == "occluded" and view.get("bbox") is not None: raise ValueError("occluded bbox must be null")
        clean.append({"side": view["side"], "visibility": view["visibility"], "bbox": bbox, "jpeg_sha256": view["jpeg_sha256"]})
    if all(v["visibility"] == "occluded" for v in clean) and body["verdict"] != "uncertain": raise ValueError("both occluded views require uncertain verdict")
    return ident, keys, float(ts), label, body["verdict"], clean

def save_example(client, bucket, cache_root, site, date, body, curator, now=None):
    calibration_id, keys, ts, label, verdict, views = _example_body(site, date, body)
    cal = _stored_calibration(client, bucket, site, calibration_id)
    if cal["camera_ids"] != [_key(k)[1] for k in keys]: raise ValueError("source cameras do not match calibration")
    extracted = frames(client, bucket, cache_root, keys[0], keys[1], ts, cal["delta_s"], cal["mode"])
    by_side = {f["side"]: f for f in extracted["frames"]}
    for view in views:
        if view["jpeg_sha256"] != by_side[view["side"]]["jpeg_sha256"]: raise ValueError("JPEG hash mismatch")
    base_time = now or datetime.now(timezone.utc)
    created = base_time.isoformat().replace("+00:00", "Z"); expires = (base_time + timedelta(days=7)).isoformat().replace("+00:00", "Z")
    ident = uuid.uuid4().hex; prefix = _example_prefix(site, date, ident)
    metadata = {"id": ident, "site": site, "date": date, "curator": curator, "calibration_id": calibration_id, "source_keys": keys, "ts": ts, "interval": extracted["interval"], "journey_label": label, "verdict": verdict, "views": views, "created_at": created, "expires_at": expires, "frames": []}
    for frame in extracted["frames"]:
        raw = base64.b64decode(frame["jpeg_data_url"].split(",", 1)[1]); side = frame["side"]
        _put(client, bucket, prefix + f"{side}.jpg", raw, "image/jpeg")
        metadata["frames"].append({k: v for k, v in frame.items() if k != "jpeg_data_url"} | {"jpeg_url": f"/api/dataset/paired/{site}/{date}/examples/{ident}/frames/{side}"})
    _put(client, bucket, prefix + "example.json", _json_bytes(metadata), "application/json")
    return metadata

def _read_example(client, bucket, site, date, ident, now=None):
    _site_date(site, date); _id(ident, "example")
    doc = _get_json(client, bucket, _example_prefix(site, date, ident) + "example.json")
    if not isinstance(doc, dict) or doc.get("id") != ident or doc.get("site") != site or doc.get("date") != date: raise RecordingUnavailable("paired example unavailable")
    if _expired(doc, now): raise RecordingMissing("paired example expired")
    return doc

def list_examples(client, bucket, site, date, curator, reviewers, now=None):
    _site_date(site, date); out = []
    prefix = f"{PAIRED_PREFIX}examples/{site}/{date}/"
    ids = sorted({k[len(prefix):].split("/", 1)[0] for k in _list_keys(client, bucket, prefix)
                  if k.startswith(prefix) and k[len(prefix):].count("/") == 1
                  and k.endswith("/example.json") and EXAMPLE_ID.fullmatch(k[len(prefix):].split("/", 1)[0])})
    for ident in ids:
        try:
            doc = _read_example(client, bucket, site, date, ident, now)
            doc["can_delete"] = doc.get("curator") == curator or curator in reviewers; out.append(doc)
        except RecordingMissing: pass
    return {"examples": out}

def get_example(client, bucket, site, date, ident, curator, reviewers, now=None):
    doc = _read_example(client, bucket, site, date, ident, now); doc["can_delete"] = doc.get("curator") == curator or curator in reviewers; return doc

def get_example_frame(client, bucket, site, date, ident, side, now=None):
    if side not in ("a", "b"): raise ValueError("invalid frame side")
    doc = _read_example(client, bucket, site, date, ident, now)
    raw = _get_object(client, bucket, _example_prefix(site, date, ident) + f"{side}.jpg", MAX_JPEG)
    expected = next((f.get("jpeg_sha256") for f in doc.get("frames", []) if f.get("side") == side), None)
    if expected != hashlib.sha256(raw).hexdigest(): raise RecordingUnavailable("paired image integrity failure")
    return raw

def delete_example(client, bucket, site, date, ident, curator, reviewers, now=None):
    doc = _read_example(client, bucket, site, date, ident, now)
    if doc.get("curator") != curator and curator not in reviewers: raise PermissionError("example deletion forbidden")
    prefix = _example_prefix(site, date, ident)
    try:
        _delete(client, bucket, prefix + "example.json")
        for side in ("a", "b"): _delete(client, bucket, prefix + side + ".jpg")
    except Exception: raise
    return {"ok": True}

def _delete(client, bucket, key):
    try: client.delete_object(Bucket=bucket, Key=key)
    except Exception as exc: raise _provider_error(exc) from None

def cleanup(client, bucket, cache_root, now=None):
    now = now or datetime.now(timezone.utc); prefix = PAIRED_PREFIX + "examples/"; grouped = {}
    try:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []): grouped.setdefault("/".join(obj["Key"].split("/")[:6]), []).append(obj)
    except Exception as exc: raise _provider_error(exc) from None
    removed = 0
    for group, objects in grouped.items():
        parts = group.split("/")
        if len(parts) != 6 or parts[:3] != PAIRED_PREFIX.rstrip("/").split("/") + ["examples"] or not CAM.fullmatch(parts[3]) or not DATE.fullmatch(parts[4]) or not EXAMPLE_ID.fullmatch(parts[5]): continue
        marker = next((o for o in objects if o["Key"] == group + "/example.json"), None)
        expired = False
        if marker:
            try: expired = _expired(_get_json(client, bucket, marker["Key"]), now)
            except RecordingMissing: continue
            except RecordingUnavailable: continue
        else:
            newest = [o.get("LastModified") for o in objects if o.get("LastModified")]
            expired = bool(newest and now - max(newest) >= timedelta(days=7))
        if expired:
            allowed = {group + "/a.jpg", group + "/b.jpg", group + "/example.json"}
            for obj in objects:
                if obj["Key"] in allowed: _delete(client, bucket, obj["Key"])
            removed += 1
    with PAIR_LOCK: _trim_cache(cache_root)
    return {"removed": removed}
