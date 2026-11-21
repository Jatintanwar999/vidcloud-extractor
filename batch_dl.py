#!/usr/bin/env python3
"""
batch_dl.py - turn an exported lecture list into video files.

This is the consumer for the JSON the scraper agent hands back. Feed it a
file with lecture records and it produces one video per lecture, resuming
and skipping work it has already done.

    python3 batch_dl.py jee360_links.json
    python3 batch_dl.py jee360_links.json --dry-run
    python3 batch_dl.py jee360_links.json --limit 3
    python3 batch_dl.py jee360_links.json --only "IUPAC,Rescheduled"
    python3 batch_dl.py jee360_links.json --outdir /root/lectures

Input tolerance
---------------
The scraper will not hand back exactly the field names you asked for, so this
accepts the common variants (snake_case, camelCase, and the raw data-*
attribute names). A record needs either a ready play.php URL or enough IDs to
build one:

    video_id  (+ typeId, batch_id, subject_id, topic_id)

`.mpd` URLs are recognised and ignored on purpose: they are a decoy and
return MissingKey.

Safety
------
* Disk guard: refuses to start a lecture unless --min-free-mb is available.
  This machine shares a volume with the host and regularly sits at 100%.
* Disk-full mid-download is caught and the lecture is marked failed, not
  silently truncated.
* A manifest records status per lecture so a re-run skips finished files and
  retries failures.

Verified end to end against the real site: one lecture downloaded, remuxed to
MP4 (H.264 + AAC) and passed `ffmpeg -f null -` with exit 0.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import vidcloud_dl  # noqa: E402  (same directory)

# ------------------------------------------------------------------ fields --
# First match wins. Order matters: most specific first.
URL_KEYS = ("unverified_player_url", "play_url", "playpage_url",
            "play_php_url", "player_url", "url", "link", "href")

ID_KEYS = {
    "video_id": ("video_id", "videoId", "videoid", "data-video-id"),
    "typeId": ("typeId", "type_id", "typeid", "data-type-id"),
    "batch_id": ("batch_id", "batchId", "batchid", "data-batch-id"),
    "subject_id": ("subject_id", "subjectId", "subjectid", "data-subject-id"),
    "topic_id": ("topic_id", "topicId", "topicid", "data-topic-id"),
}

TITLE_KEYS = ("title", "lecture_title", "lecture", "name",
              "chapter", "video_name", "topic")
DECOY_KEYS = ("decoy_mpd", "mpd", "mpd_url", "video_url")

BASE = "https://vidcloud.eu.org/play.php"


def first(rec, keys):
    for k in keys:
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return ""


def find_records(data):
    """Pull the list of lecture dicts out of whatever shape the JSON has."""
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        # A single record?
        if any(first(data, k) for k in (URL_KEYS + ID_KEYS["video_id"])):
            return [data]
        # Otherwise the first list-of-dicts value wins.
        best = []
        for v in data.values():
            if isinstance(v, list):
                cand = [r for r in v if isinstance(r, dict)]
                if len(cand) > len(best):
                    best = cand
        if best:
            return best
        for v in data.values():
            if isinstance(v, dict):
                cand = find_records(v)
                if len(cand) > len(best):
                    best = cand
        return best
    return []


def build_url(rec):
    """Return a play.php URL, or ("", reason) if this record cannot yield one."""
    url = first(rec, URL_KEYS)
    if url:
        if ".mpd" in url.lower():
            return "", "record only carries a decoy .mpd URL"
        if "play.php" not in url:
            return "", "url is not a play.php link"
        return url, ""
    ids = {name: first(rec, keys) for name, keys in ID_KEYS.items()}
    missing = [n for n in ("video_id", "typeId") if not ids[n]]
    if missing:
        return "", "missing %s (and no play.php URL)" % ", ".join(missing)
    q = [("batch_id", ids["batch_id"]), ("subject_id", ids["subject_id"]),
         ("topic_id", ids["topic_id"]), ("video_id", ids["video_id"]),
         ("typeId", ids["typeId"]), ("play_type", "Lecture"),
         # video_type=new is REQUIRED: without it play.php returns every
         # #enc_* field as "" and nothing can be decrypted. It is a routing
         # hint the server checks, not a cosmetic param.
         ("video_type", "new")]
    return BASE + "?" + "&".join("%s=%s" % (k, v) for k, v in q if v), ""


def safe_name(text, fallback, maxlen=90):
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = fallback
    return text[:maxlen].strip(" .")


def free_mb(path):
    st = shutil.disk_usage(path)
    return st.free // (1024 * 1024)


def load_manifest(path):
    if os.path.exists(path):
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            print("! manifest unreadable, starting fresh")
    return {}


def save_manifest(path, man):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(man, fh, indent=2)
    os.replace(tmp, path)


def verify(path):
    """Is the produced file decodable? Returns (ok, detail)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return bool(os.path.getsize(path) > 100000), "size-only check"
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=180)
    except Exception as e:  # noqa: BLE001
        return False, "ffprobe failed: %s" % e
    if out.returncode != 0 or not out.stdout.strip():
        return False, (out.stderr or "").strip()[:200] or "ffprobe error"
    try:
        dur = float(out.stdout.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return False, "unreadable duration"
    if dur <= 1:
        return False, "duration %.1fs looks empty" % dur
    return True, "%.0f s" % dur


# -------------------------------------------------------------------- main --

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="download every lecture in an exported JSON")
    ap.add_argument("json_file")
    ap.add_argument("-o", "--outdir", default="videos")
    ap.add_argument("--limit", type=int, default=0,
                    help="only process N lectures")
    ap.add_argument("--only", default="",
                    help="comma-separated substrings to match against titles")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan, download nothing")
    ap.add_argument("--force", action="store_true",
                    help="redownload even if the manifest says done")
    ap.add_argument("--min-free-mb", type=int, default=1200,
                    help="stop if free disk drops below this (default 1200)")
    ap.add_argument("--max-seconds", type=float, default=0,
                    help="download only the first N seconds per lecture")
    a = ap.parse_args(argv)

    with open(a.json_file) as fh:
        data = json.load(fh)

    records = find_records(data)
    if not records:
        raise SystemExit("no lecture records found in %s" % a.json_file)

    # Normalise into jobs, reporting anything unusable up front.
    jobs, unusable = [], []
    for i, rec in enumerate(records, 1):
        url, why = build_url(rec)
        title = safe_name(first(rec, TITLE_KEYS), "lecture-%03d" % i)
        if not url:
            unusable.append((title, why))
            continue
        key = first(rec, ID_KEYS["video_id"]) or url
        jobs.append({"key": key, "title": title, "url": url, "index": i})

    print("records : %d" % len(records))
    print("usable  : %d" % len(jobs))
    if unusable:
        print("skipped : %d" % len(unusable))
        for title, why in unusable[:10]:
            print("   - %s: %s" % (title, why))
        if len(unusable) > 10:
            print("   ... and %d more" % (len(unusable) - 10))

    if a.only:
        needles = [s.strip().lower() for s in a.only.split(",") if s.strip()]
        jobs = [j for j in jobs
                if any(n in j["title"].lower() for n in needles)]
        print("filter  : %d match --only" % len(jobs))

    if a.limit:
        jobs = jobs[:a.limit]

    os.makedirs(a.outdir, exist_ok=True)
    manifest_path = os.path.join(a.outdir, "manifest.json")
    manifest = {} if a.force else load_manifest(manifest_path)

    todo = [j for j in jobs
            if manifest.get(j["key"], {}).get("status") != "done"]
    print("pending : %d  (of %d; %d already done)"
          % (len(todo), len(jobs), len(jobs) - len(todo)))

    if a.dry_run:
        print("\n--- plan ---")
        for j in todo:
            name = "%03d - %s.mp4" % (j["index"], j["title"])
            print("  %s\n      %s" % (name, j["url"][:110]))
        print("\nfree disk: %d MB" % free_mb(a.outdir))
        return 0

    failures = []
    for n, job in enumerate(todo, 1):
        name = "%03d - %s.mp4" % (job["index"], job["title"])
        dest = os.path.join(a.outdir, name)

        print("\n[%d/%d] %s" % (n, len(todo), name))

        free = free_mb(a.outdir)
        if free < a.min_free_mb:
            print("  ! only %d MB free (need %d) - stopping so nothing is "
                  "left half-written" % (free, a.min_free_mb))
            failures.append((name, "disk full"))
            break

        if os.path.exists(dest) and os.path.getsize(dest) > 100000:
            ok, detail = verify(dest)
            if ok and not a.force:
                print("  already present and valid (%s)" % detail)
                manifest[job["key"]] = {"title": job["title"],
                                        "status": "done",
                                        "file": name}
                save_manifest(manifest_path, manifest)
                continue

        t0 = time.time()
        try:
            # Run the sibling script by absolute path: `python3 -m vidcloud_dl`
            # only resolves when the repo root happens to be the cwd.
            script = os.path.join(HERE, "vidcloud_dl.py")
            cmd = [sys.executable, script, job["url"], "-o", dest]
            if a.max_seconds:
                cmd += ["--max-seconds", str(a.max_seconds)]
            rc = subprocess.call(cmd)
        except OSError as e:
            rc = 1
            print("  ! filesystem error: %s" % e)
        except Exception:  # noqa: BLE001
            rc = 1
            traceback.print_exc()

        if rc != 0 or not os.path.exists(dest):
            print("  FAILED (exit %s)" % rc)
            failures.append((name, "exit %s" % rc))
            manifest[job["key"]] = {"title": job["title"], "status": "failed",
                                    "url": job["url"], "error": "exit %s" % rc}
            save_manifest(manifest_path, manifest)
            continue

        ok, detail = verify(dest)
        size = os.path.getsize(dest) / 1e6
        if ok:
            print("  done: %.1f MB, %s, %.0fs" % (size, detail, time.time() - t0))
            manifest[job["key"]] = {"title": job["title"], "status": "done",
                                    "file": name, "mb": round(size, 1),
                                    "duration": detail}
        else:
            print("  FAILED verification: %s" % detail)
            failures.append((name, detail))
            manifest[job["key"]] = {"title": job["title"], "status": "failed",
                                    "url": job["url"], "error": detail,
                                    "file": name}
        save_manifest(manifest_path, manifest)

    done = sum(1 for v in manifest.values() if v.get("status") == "done")
    print("\n" + "=" * 60)
    print("done in manifest : %d" % done)
    print("failed this run  : %d" % len(failures))
    for name, why in failures[:15]:
        print("   - %s: %s" % (name, why))
    print("free disk        : %d MB" % free_mb(a.outdir))
    print("manifest         : %s" % manifest_path)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
