#!/usr/bin/env python3
"""
grab_hls.py - download a vidcloud HLS stream WITHOUT local disk pressure.

Why this exists
---------------
The dev container this project was built in has a 338 GB volume that is shared
with the host and sits at ~100% full. A 180 MB lecture will not fit there, and
the download repeatedly died at "No space left on device".

Solution: run this on a GitHub Actions runner (14 GB+ free, ffmpeg preinstalled)
and publish the result as a Release asset instead of downloading it locally.

What it does
------------
1. extract() the real signed playlist URL from the play.php page (AES-128-CBC),
2. fetch the media playlist,
3. download every segment with the Referer header the CDN requires,
4. byte-concatenate the segments -> one valid MPEG-TS file
   (raw MPEG-TS segments concatenate as-is; there is no init segment here),
5. leave remuxing to ffmpeg in the workflow (MPEG-TS -> MP4 is lossless copy).

Usage
-----
    python3 tools/grab_hls.py "<play.php url>" -o build/full.ts
    python3 tools/grab_hls.py "<play.php url>" -o build/full.ts --max-seconds 60
"""

import argparse
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from vidcloud_extract import extract, http_get  # noqa: E402

REFERER = "https://vidcloud.eu.org/"
CHUNK = 1 << 20


def parse_playlist(text, base):
    """Return [(duration, absolute_url), ...] for a media playlist."""
    out, dur = [], 0.0
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("#EXTINF:"):
            dur = float(ln.split(":", 1)[1].rstrip(",").split(",")[0])
        elif ln and not ln.startswith("#"):
            out.append((dur, urllib.parse.urljoin(base, ln)))
            dur = 0.0
    return out


def download_segment(url, fh):
    with http_get(url, {"Referer": REFERER}, timeout=120) as r:
        n = 0
        while True:
            chunk = r.read(CHUNK)
            if not chunk:
                break
            fh.write(chunk)
            n += len(chunk)
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="download a vidcloud HLS stream to one .ts file")
    ap.add_argument("play_url")
    ap.add_argument("-o", "--output", default="build/full.ts")
    ap.add_argument("--max-seconds", type=float, default=0.0,
                    help="stop after this many seconds of media (0 = everything)")
    a = ap.parse_args(argv)

    info = extract(a.play_url)
    print(f"title   : {info['title']}")
    print(f"drm     : {info['drm'] or '-'}")
    print(f"playlist: {info['media_url'][:100]}...")

    playlist = http_get(info["media_url"], {"Referer": REFERER}, timeout=60).read().decode()
    segs = parse_playlist(playlist, info["media_url"])
    if not segs:
        raise SystemExit("no segments in playlist - is this a master playlist?")
    total_dur = sum(d for d, _ in segs)
    print(f"segments: {len(segs)}  duration: {total_dur/60:.1f} min")

    os.makedirs(os.path.dirname(a.output) or ".", exist_ok=True)

    written = 0
    done_dur = 0.0
    t0 = time.time()
    with open(a.output, "wb") as fh:
        for i, (dur, url) in enumerate(segs, 1):
            if a.max_seconds and done_dur >= a.max_seconds:
                print(f"  reached --max-seconds={a.max_seconds:g}s, stopping")
                break
            n = download_segment(url, fh)
            written += n
            done_dur += dur
            pct = 100.0 * done_dur / total_dur
            print(f"  seg {i:>2}/{len(segs)}  {n/1e6:6.1f} MB  "
                  f"total {written/1e6:7.1f} MB  {pct:5.1f}%  "
                  f"({time.time()-t0:5.0f}s)", flush=True)

    print(f"wrote {a.output}: {written} bytes, ~{done_dur/60:.1f} min of media")
    if written < 1024:
        raise SystemExit("output suspiciously small - download failed")

    # sanity: every MPEG-TS packet is 188 bytes
    if written % 188 != 0:
        print(f"WARNING: size {written} is not a multiple of 188 (TS packet size)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
