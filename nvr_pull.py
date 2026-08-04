#!/usr/bin/env python3
"""
Dexterity NVR extraction service.

Pulls recorded footage off Hikvision PoE NVRs over ISAPI, verifies it,
and lands it on the rig's ingest tree ready for the DeepStream batch:

    <out_dir>/<YYYY-MM-DD>/<site>/<camN>/<start>-<end>.mkv
    <out_dir>/<YYYY-MM-DD>/<site>/manifest.json      (sha256 + durations)
    <out_dir>/<YYYY-MM-DD>/<site>/.verified          (written only if ALL checks pass)

Nothing is ever deleted from an NVR by this tool. The operator clears an
NVR's disk manually ONLY after the site-day's `.verified` marker exists.

Usage:
    python3 nvr_pull.py --config config.yaml --date 2026-08-04 \
        --start 05:00 --end 21:30 [--site site1-greatnorth] [--dry-run]

Requires: python3-requests, ffmpeg/ffprobe on PATH.
"""

import argparse, hashlib, json, re, subprocess, sys, threading, time
from datetime import datetime
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth

try:
    import yaml
except ImportError:
    yaml = None

ISAPI_SEARCH = "http://{host}/ISAPI/ContentMgmt/search"
ISAPI_DOWNLOAD = "http://{host}/ISAPI/ContentMgmt/download"
NS = "http://www.hikvision.com/ver20/XMLSchema"

SEARCH_BODY = """<?xml version="1.0" encoding="utf-8"?>
<CMSearchDescription>
  <searchID>{sid}</searchID>
  <trackList><trackID>{track}</trackID></trackList>
  <timeSpanList>
    <timeSpan>
      <startTime>{start}</startTime>
      <endTime>{end}</endTime>
    </timeSpan>
  </timeSpanList>
  <maxResults>100</maxResults>
  <searchResultPosition>{pos}</searchResultPosition>
  <metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>
</CMSearchDescription>"""

DOWNLOAD_BODY = """<?xml version="1.0"?>
<downloadRequest>
  <playbackURI>{uri}</playbackURI>
</downloadRequest>"""


def log(site, msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{site}] {msg}", flush=True)


def isapi_time(date, hhmm):
    return f"{date}T{hhmm}:00Z"


def search_recordings(sess, host, track, date, start, end, site):
    """Return list of (playbackURI, startTime, endTime) for one channel."""
    results, pos = [], 0
    sid = f"dex-{track}-{int(time.time())}"
    while True:
        body = SEARCH_BODY.format(sid=sid, track=track,
                                  start=isapi_time(date, start),
                                  end=isapi_time(date, end), pos=pos)
        r = sess.post(ISAPI_SEARCH.format(host=host), data=body, timeout=30)
        r.raise_for_status()
        text = r.text
        uris = re.findall(r"<playbackURI>(.*?)</playbackURI>", text, re.S)
        starts = re.findall(r"<startTime>(.*?)</startTime>", text, re.S)
        ends = re.findall(r"<endTime>(.*?)</endTime>", text, re.S)
        batch = list(zip([u.strip().replace("&amp;", "&") for u in uris],
                         [s.strip() for s in starts], [e.strip() for e in ends]))
        results.extend(batch)
        if "<responseStatusStrg>MORE</responseStatusStrg>" in text and batch:
            pos += len(batch)
        else:
            break
    log(site, f"track {track}: {len(results)} segment(s) found")
    return results


def download_segment(sess, host, uri, dest, site):
    """Stream one recorded segment to disk. Returns bytes written."""
    body = DOWNLOAD_BODY.format(uri=uri)
    with sess.post(ISAPI_DOWNLOAD.format(host=host), data=body,
                   stream=True, timeout=60) as r:
        r.raise_for_status()
        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
        return written


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=120)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def remux_to_mkv(src, dest):
    """Container-normalise without re-encoding. Returns True on success."""
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
                        "-c", "copy", str(dest)],
                       capture_output=True, text=True)
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def ts_slug(iso):
    return re.sub(r"[:TZ-]", "", iso)[:14]


def pull_site(nvr, date, start, end, out_root, remux, dry):
    site = nvr["name"]
    host = nvr["host"]
    sess = requests.Session()
    sess.auth = HTTPDigestAuth(nvr["user"], nvr["password"])
    sess.headers["Content-Type"] = "application/xml"

    site_dir = out_root / date / site
    manifest = {"site": site, "date": date, "window": f"{start}-{end}",
                "pulled_at": datetime.now().isoformat(), "files": []}
    all_ok = True

    for ch in nvr["channels"]:
        track, label = ch["id"], ch["label"]
        try:
            segments = search_recordings(sess, host, track, date, start, end, site)
        except Exception as e:
            log(site, f"track {track}: SEARCH FAILED: {e}")
            all_ok = False
            continue
        if not segments:
            log(site, f"track {track}: NO RECORDINGS in window — check NVR schedule")
            all_ok = False
            continue

        cam_dir = site_dir / label
        cam_dir.mkdir(parents=True, exist_ok=True)
        for uri, seg_start, seg_end in segments:
            base = f"{ts_slug(seg_start)}-{ts_slug(seg_end)}"
            raw = cam_dir / f"{base}.mp4"
            final = cam_dir / f"{base}.mkv" if remux else raw
            if final.exists() and final.stat().st_size > 0:
                log(site, f"{label}/{base}: exists, skipping")
                continue
            if dry:
                log(site, f"{label}/{base}: would download {uri[:60]}...")
                continue
            t0 = time.time()
            try:
                nbytes = download_segment(sess, host, uri, raw, site)
            except Exception as e:
                log(site, f"{label}/{base}: DOWNLOAD FAILED: {e}")
                all_ok = False
                continue
            rate = nbytes / max(time.time() - t0, 0.1) / 1e6
            log(site, f"{label}/{base}: {nbytes/1e9:.2f} GB at {rate:.0f} MB/s")

            if remux:
                if remux_to_mkv(raw, final):
                    raw.unlink()
                else:
                    log(site, f"{label}/{base}: remux failed — keeping original container")
                    final = raw

            dur = ffprobe_duration(final)
            digest = sha256_file(final)
            ok = dur > 0
            if not ok:
                all_ok = False
                log(site, f"{label}/{base}: VERIFY FAILED (unreadable)")
            manifest["files"].append({
                "camera": label, "file": str(final.relative_to(site_dir)),
                "bytes": final.stat().st_size, "duration_s": round(dur, 1),
                "sha256": digest, "ok": ok,
                "segment": {"start": seg_start, "end": seg_end}})

    if not dry:
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        if all_ok and manifest["files"]:
            (site_dir / ".verified").write_text(datetime.now().isoformat())
            log(site, "SITE-DAY VERIFIED — safe to clear this NVR after review")
        else:
            log(site, "NOT VERIFIED — do NOT clear this NVR; investigate and re-run")
    return all_ok


def main():
    ap = argparse.ArgumentParser(description="Pull recorded footage off Hikvision NVRs")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--start", default="05:00")
    ap.add_argument("--end", default="21:30")
    ap.add_argument("--site", help="pull only this site (config name)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if yaml is None:
        sys.exit("pip install pyyaml requests")
    cfg = yaml.safe_load(Path(args.config).read_text())
    out_root = Path(cfg.get("out_dir", "./ingest"))
    remux = bool(cfg.get("remux_mkv", True))
    nvrs = [n for n in cfg["nvrs"] if not args.site or n["name"] == args.site]
    if not nvrs:
        sys.exit(f"no NVR named {args.site} in config")

    threads, results = [], {}
    for nvr in nvrs:
        t = threading.Thread(
            target=lambda n=nvr: results.update(
                {n["name"]: pull_site(n, args.date, args.start, args.end,
                                      out_root, remux, args.dry_run)}))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    print("\n=== SUMMARY ===")
    for name, ok in results.items():
        print(f"  {name}: {'VERIFIED' if ok else 'ATTENTION NEEDED'}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
