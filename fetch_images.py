#!/usr/bin/env python3
"""Download CC-licensed images for a class from Wikimedia Commons (no API key).

  python fetch_images.py f-abnormal "abnormal load truck" [--max 200]
  python fetch_images.py a-motorcycle "motorcycle road traffic"

The road gives us cars by the thousand and abnormal loads by the month, so the classes
the traffic will not supply come off the web instead. Images land in
dataset/external/<class>/ with their attribution in sources.jsonl — the licence requires
crediting the author, and a file with no record of where it came from cannot be credited.

Next: python ingest_images.py dataset/external/<class> --as <class>
"""

import hashlib
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXTERNAL = ROOT / "dataset" / "external"
API = "https://commons.wikimedia.org/w/api.php"
# Wikimedia blocks anonymous bulk clients: the UA must name the tool and a way to reach it.
UA = "FieldKit-dataset/1.0 (https://github.com/bandajon/fieldkit)"
# Lowercased prefixes: "CC BY 4.0", "CC BY-SA 3.0", "CC0", "Public domain". Anything else
# (GFDL, fair use, "no licence") is not ours to redistribute, so it never lands.
OK_LICENSE = ("cc by", "cc0", "public domain")
OK_MIME = ("image/jpeg", "image/png")
MIN_WIDTH = 480          # below this a vehicle is a smudge the curator cannot box
PAGE = 50                # the API's generator limit for anonymous callers
PAUSE = 0.5              # between requests, per the etiquette guidelines


def search(terms, offset):
    q = {"action": "query", "generator": "search", "gsrsearch": terms, "gsrnamespace": 6,
         "gsrlimit": PAGE, "gsroffset": offset, "prop": "imageinfo",
         "iiprop": "url|mime|extmetadata", "iiurlwidth": 1280, "format": "json"}
    req = urllib.request.Request(f"{API}?{urllib.parse.urlencode(q)}",
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def meta(ii, key):
    return (ii.get("extmetadata") or {}).get(key, {}).get("value", "")


def fetch(cls, terms, cap):
    out = EXTERNAL / cls
    out.mkdir(parents=True, exist_ok=True)
    log = out / "sources.jsonl"
    kept = skipped = 0
    offset = 0
    while kept < cap:
        pages = (search(terms, offset).get("query") or {}).get("pages") or {}
        if not pages:
            break                       # the search ran out before the cap did
        offset += PAGE
        for p in pages.values():
            if kept >= cap:
                break
            ii = (p.get("imageinfo") or [{}])[0]
            url = ii.get("thumburl") or ii.get("url")
            lic = meta(ii, "LicenseShortName")
            if not url or not lic.lower().startswith(OK_LICENSE):
                skipped += 1
                continue
            if (ii.get("thumbmime") or ii.get("mime")) not in OK_MIME:
                skipped += 1
                continue
            if int(ii.get("thumbwidth") or ii.get("width") or 0) < MIN_WIDTH:
                skipped += 1
                continue
            time.sleep(PAUSE)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=60) as r:
                    blob = r.read()
            except Exception as e:      # one dead thumbnail must not end the run
                print(f"  ! {url}: {e}")
                skipped += 1
                continue
            name = hashlib.sha1(blob).hexdigest()[:12] + ".jpg"
            f = out / name
            if f.exists():              # same bytes from another query: already ours
                continue
            f.write_bytes(blob)
            with log.open("a") as fh:
                fh.write(json.dumps({"file": name, "url": url,
                                     "page": ii.get("descriptionurl") or p.get("title", ""),
                                     "license": lic, "author": meta(ii, "Artist"),
                                     "query": terms}) + "\n")
            kept += 1
            print(f"  v {name}  {lic}", flush=True)
        time.sleep(PAUSE)
    print(f"\n{kept} image(s) into {out} ({skipped} skipped: licence, format or too small)")
    print(f"attribution: {log}")
    print(f"run: python ingest_images.py {out} --as {cls}")
    return kept


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--max"]
    cap = 200
    if "--max" in sys.argv:
        cap, args = int(args[-1]), args[:-1]
    if len(args) != 2:
        sys.exit(__doc__)
    fetch(args[0], args[1], cap)
