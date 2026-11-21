# AGENT_GUIDE.md

Instructions for an agent that has scraped lecture data and needs to turn it
into video files with this repository.

Read this before running anything. One wrong field name wastes a whole run.

---

## 1. What you must scrape

The downloader needs a **play.php URL**, or the IDs to build one. Only these
five values are load-bearing:

| Field | Where it lives | Example |
|---|---|---|
| `batch_id` | ancestor element / page URL | `6a86cab0a06c46001b1bd7ac` |
| `subject_id` | ancestor element / page URL | `6a870494b9312899f0a0fba4` |
| `topic_id` | ancestor element / page URL | `6a882c27fac50e4673031281` |
| `video_id` | `data-video-id` on the lecture card | `6a882c3169d413b29e5eb0eb` |
| `typeId` | `data-type-id` on the lecture card | `697a102215179c844776f542` |

Also worth capturing (cosmetic, improves filenames):

| Field | Where | Notes |
|---|---|---|
| `title` | card text | becomes the filename |
| `chapter` | heading above the card | organisation only |

### Things that are NOT usable

- **`.mpd` URLs** — a decoy. The CloudFront distribution is private and
  returns `MissingKey`. Storing one wastes a slot.
- **HLS `.m3u8` / CDN URLs** — expire 240 minutes after the page loads. Useless
  as stored data.
- **`video_name` / `video_img` / `video_url`** — cosmetic page-render params.
  Optional; omitting them is fine.

### The one gotcha that will break you

**`video_type=new` is required.** Without it, `play.php` returns every
`#enc_*` input as `value=""` — empty, not missing. It looks exactly like a
layout change and it is not.

`batch_dl.py` and `vidcloud_dl.py` both add it automatically now, so you do
not have to. But if you hand-build a URL for any other purpose, include it:

```
&video_type=new
```

If you ever see `the page returned an EMPTY #enc_video_url`, the cause is
one of:
1. `video_type=new` missing (auto-fixed now),
2. IDs mixed from different lecture cards,
3. a stale/expired batch or subject ID.

---

## 2. What to output

A JSON array. One object per lecture. Field names are flexible —
`batch_dl.py` accepts snake_case, camelCase, and raw `data-*` names — but
this is the canonical shape:

```json
[
  {
    "title": "Mechanical Properties of Fluids - L01",
    "chapter": "Ch - 12: Mechanical Properties of Fluids",
    "teacher": "Rajwant Sir",
    "batch_id":   "6a86cab0a06c46001b1bd7ac",
    "subject_id": "6a870494b9312899f0a0fba4",
    "topic_id":   "6a882c27fac50e4673031281",
    "video_id":   "6a882c3169d413b29e5eb0eb",
    "typeId":     "697a102215179c844776f542",
    "play_type":  "Lecture",
    "ids_source": "lecture_page"
  }
]
```

`ids_source` is for auditing: `"listing"` if the IDs came off the listing
card, `"lecture_page"` if you had to open the lecture to get them. Do not
invent it.

You may instead supply a ready URL, which skips assembly:

```json
{ "title": "...", "play_url": "https://vidcloud.eu.org/play.php?...&video_type=new" }
```

Recognised URL keys: `unverified_player_url`, `play_url`, `playpage_url`,
`play_php_url`, `player_url`, `url`, `link`, `href`.

---

## 3. Verify your export before running a download

Three cheap checks catch nearly every bad export.

```bash
# 1. No duplicate video_id - a repeat means a loop variable was reused,
#    which silently returns the SAME lecture for different titles.
python3 -c "
import json,collections
d=json.load(open('lectures.json'))
ids=[r.get('video_id') or r.get('videoId') for r in d]
dup=[k for k,v in collections.Counter(ids).items() if v>1 and k]
print('records:',len(d)); print('duplicate video_ids:',dup or 'none')
"

# 2. Every record resolves to a URL
python3 -c "
import json,sys; sys.path.insert(0,'.')
import batch_dl
for r in json.load(open('lectures.json')):
    u,why=batch_dl.build_url(r)
    print(('OK  ' if u else 'FAIL'), r.get('title','?')[:45], why)
"

# 3. Dry run - shows the plan, downloads nothing
python3 batch_dl.py lectures.json --dry-run
```

A duplicate `video_id` is the dangerous one. It produces valid-looking files
with the wrong content, and nothing downstream can detect it.

---

## 4. Download

One lecture first. Always.

```bash
# metadata only - confirms decrypt + a live signed URL
python3 vidcloud_dl.py "<play.php url>" --info

# small clip - proves the full chain
python3 vidcloud_dl.py "<play.php url>" -o test.mp4 --max-seconds 45

# confirm it is a real video
ffprobe -v error -show_entries format=duration -show_entries \
        stream=codec_name,codec_type -of default=nw=1 test.mp4
ffmpeg -v error -i test.mp4 -f null -      # exit 0 = clean
```

Only after that passes, do the batch:

```bash
python3 batch_dl.py lectures.json -o videos --dry-run
python3 batch_dl.py lectures.json -o videos --limit 2 --min-free-mb 800
python3 batch_dl.py lectures.json -o videos
```

`batch_dl.py` writes `videos/manifest.json`. A re-run skips `done` lectures
and retries `failed` ones.

### Disk

This host shares a 338 GB volume with the hypervisor and often sits at 100%.
A 68-minute lecture is ~140 MB. The default `--min-free-mb 1200` refuses to
start a lecture without headroom. Do not lower it below ~400 while space is
tight, and never run two batches at once.

If it stops with `disk full`, wait — free space fluctuates with other
tenants. Delete `videos/*.ts` leftovers and retry.

---

## 5. Expected output

```
records : 12
usable  : 12
pending : 12  (of 12; 0 already done)

[1/12] 001 - Mechanical Properties of Fluids - L01.mp4
  seg  1/43    15.8 MB  total    15.8 MB    2.4%  (  3s)
  ...
  done: 143.2 MB, 213.3 min, 312s
```

Per lecture you should see `done:` with a size and duration. `FAILED` or a
`failed this run` count means something is wrong — read the reason, do not
retry blindly.

---

## 6. Self-test any copy of the script

Before trusting a `vidcloud_dl.py`, check it is the real one:

```bash
python3 -c "import vidcloud_dl as v; print(v.decrypt_blob('4BloTF1TnAw/nwFoOrwx5A=='))"
```

| Output | Meaning |
|---|---|
| `clearkey` | correct |
| `None` | stub — decryption is a `pass`, file is gutted |
| Traceback / URL | a different version, inspect it |

A correct `vidcloud_dl.py` is **~15 KB**. A ~2.3 KB file is the stub that
silently does nothing.

---

## 7. Failure table

| Symptom | Cause | Fix |
|---|---|---|
| `EMPTY #enc_video_url` | `video_type=new` dropped | add it; script now auto-retries |
| `no #enc_video_url on the page` | old script version | update to current `main` |
| `403` on media URL | UA contains `Chrome/<n>` | use the script's UA; do not set your own |
| `403` on play.php | empty User-Agent | send any non-empty UA |
| `MissingKey` | you used the decoy `.mpd` | use `video_id` + `typeId` instead |
| URL worked, now 403 | 240 min expiry | re-fetch the page |
| `duration 0s` / tiny file | truncated download | re-run; manifest retries it |
| Two lectures, same video | duplicate `video_id` | re-scrape, do not patch |

---

## 8. Do not

- Do not store decrypted/CDN URLs as data — they expire in 240 minutes.
- Do not guess or pattern-build an ID. An unresolved lecture reported honestly
  is worth more than a fabricated one.
- Do not script around a 403 by trying UA strings at random. The rule is
  specific: Referer `https://vidcloud.eu.org/` and a UA with no
  `Chrome/<version>` token.
- Do not run a whole batch before one lecture has downloaded and passed
  `ffmpeg -f null -`.
