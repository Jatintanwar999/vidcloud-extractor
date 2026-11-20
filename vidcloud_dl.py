#!/usr/bin/env python3
"""
vidcloud_dl.py - give it a play.php URL, get a video file.

    python3 vidcloud_dl.py "https://vidcloud.eu.org/play.php?..." 
    python3 vidcloud_dl.py "<url>" -o lecture.mp4
    python3 vidcloud_dl.py "<url>" --max-seconds 60      # short preview
    python3 vidcloud_dl.py "<url>" --info                # print info, no download

It handles both shapes the CDN can return:

  * a progressive MP4 that wears a fake extension (.jpeg / .pdf / .docx), and
  * an HLS playlist (also fake-named), whose segments are raw MPEG-TS.

The URL is decided by sniffing the response bytes, never by the extension.

Dependencies
------------
Python 3.8+ standard library only. ffmpeg is optional: it is used to remux
HLS (MPEG-TS) into MP4. Without it you still get a playable .ts file.

How it works
------------
play.php hides the real link in a base64 input, encrypted with AES-128-CBC
using a key/IV hardcoded in the site's JavaScript. Decrypt it, and you get a
signed CDN URL. That URL is Referer-gated and expires 240 minutes after the
page loads, so always decrypt fresh.
"""

import argparse
import base64
import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --- recovered from the site's JS -------------------------------------------
KEY_HEX = "5237406b503423784c39216d51322476"   # 16 raw bytes, NOT a passphrase
IV_STR = "T3!nW8$qZ5@rK1#p"

# The CDN's WAF 403s any UA containing a "Chrome/<version>" token, so this one
# deliberately has none. A *correct* browser UA is blocked here.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Safari/537.36")
REFERER = "https://vidcloud.eu.org/"


# ------------------------------------------------------------------ AES-128 --
# Small dependency-free implementation; only decryption is needed.

def _tables():
    sbox = [0] * 256
    p = q = 1
    while True:
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = (q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6)) ^
             ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4))) & 0xFF
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i

    def mul(a, b):
        r = 0
        while b:
            if b & 1:
                r ^= a
            a = ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1) & 0xFF
            b >>= 1
        return r

    return (sbox, inv, [mul(i, 9) for i in range(256)],
            [mul(i, 11) for i in range(256)],
            [mul(i, 13) for i in range(256)],
            [mul(i, 14) for i in range(256)])


SBOX, INV_SBOX, M9, M11, M13, M14 = _tables()
RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _expand_key(key):
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = [SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    return [sum(w[4 * r:4 * r + 4], []) for r in range(nr + 1)], nr


def _inv_shift_rows(s):
    t = [0] * 16
    for c in range(4):
        for r in range(4):
            t[c * 4 + r] = s[((c - r) % 4) * 4 + r]
    return t


def _inv_mix_columns(t):
    n = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = t[c * 4:c * 4 + 4]
        n[c * 4 + 0] = M14[a0] ^ M11[a1] ^ M13[a2] ^ M9[a3]
        n[c * 4 + 1] = M9[a0] ^ M14[a1] ^ M11[a2] ^ M13[a3]
        n[c * 4 + 2] = M13[a0] ^ M9[a1] ^ M14[a2] ^ M11[a3]
        n[c * 4 + 3] = M11[a0] ^ M13[a1] ^ M9[a2] ^ M14[a3]
    return n


def aes_cbc_decrypt(data, key, iv):
    rk, nr = _expand_key(key)
    out = bytearray()
    prev = iv
    for off in range(0, len(data) - len(data) % 16, 16):
        blk = data[off:off + 16]
        s = [blk[i] ^ rk[nr][i] for i in range(16)]
        for rnd in range(nr - 1, -1, -1):
            s = _inv_shift_rows(s)
            s = [INV_SBOX[b] for b in s]
            s = [s[i] ^ rk[rnd][i] for i in range(16)]
            if rnd:
                s = _inv_mix_columns(s)
        out += bytes(a ^ b for a, b in zip(s, prev))
        prev = blk
    return bytes(out)


def _unpad(b):
    if b and 1 <= b[-1] <= 16 and b.endswith(bytes([b[-1]]) * b[-1]):
        return b[:-b[-1]]
    return b


def decrypt_blob(b64_blob, key_hex=KEY_HEX, iv_str=IV_STR):
    raw = base64.b64decode(b64_blob.strip())
    pt = aes_cbc_decrypt(raw, bytes.fromhex(key_hex), iv_str.encode())
    return _unpad(pt).decode("utf-8", "replace").rstrip("\x00\r\n ")


# --------------------------------------------------------------------- HTTP --

def http_get(url, headers=None, timeout=60):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_text(url, headers=None, timeout=60):
    with http_get(url, headers, timeout) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc, "replace")


def page_field(html, field, default=""):
    m = re.search(r'id="%s"[^>]*value="([^"]*)"' % re.escape(field), html)
    return _html.unescape(m.group(1)) if m else default


def extract(play_url):
    page = fetch_text(play_url, {"Referer": REFERER})
    origin = "{0.scheme}://{0.netloc}/".format(urllib.parse.urlsplit(play_url))
    info = {
        "page": play_url,
        "title": page_field(page, "video_name"),
        "video_id": page_field(page, "video_id"),
        "poster": page_field(page, "video_img"),
        "origin": origin,
    }
    enc_url = page_field(page, "enc_video_url")
    enc_drm = page_field(page, "enc_drm_type")
    if not enc_url:
        raise SystemExit("no #enc_video_url on the page - the layout changed?")
    info["media_url"] = decrypt_blob(enc_url)
    info["drm"] = decrypt_blob(enc_drm) if enc_drm else ""
    return info


# ---------------------------------------------------------------- download --

CHUNK = 1 << 20


def sniff(url, referer=REFERER):
    """Return 'hls' or 'file' by looking at the bytes, not the file name."""
    req_headers = {"Referer": referer, "Range": "bytes=0-1023"}
    with http_get(url, req_headers, timeout=60) as r:
        head = r.read(1024)
    if head.lstrip().startswith(b"#EXTM3U"):
        return "hls"
    return "file"


def parse_playlist(text, base):
    """[(duration, absolute_url), ...] for a media playlist."""
    out, dur = [], 0.0
    for raw in text.splitlines():
        ln = raw.strip()
        if ln.startswith("#EXTINF:"):
            dur = float(ln.split(":", 1)[1].split(",")[0].rstrip(",") or 0)
        elif ln and not ln.startswith("#"):
            out.append((dur, urllib.parse.urljoin(base, ln)))
            dur = 0.0
    return out


def hls_variants(text, base):
    out, bw = [], None
    for ln in text.splitlines():
        if ln.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", ln)
            bw = int(m.group(1)) if m else 0
        elif ln and not ln.startswith("#") and bw is not None:
            out.append((bw, urllib.parse.urljoin(base, ln.strip())))
            bw = None
    return out


def download_progressive(url, out, referer=REFERER):
    done = os.path.getsize(out) if os.path.exists(out) else 0
    headers = {"Referer": referer}
    if done:
        headers["Range"] = "bytes=%d-" % done
    with http_get(url, headers, timeout=120) as r:
        total = int(r.headers.get("Content-Length") or 0) + done
        mode = "ab" if r.status == 206 else "wb"
        if mode == "wb":
            done = 0
        with open(out, mode) as f:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    sys.stderr.write("\r  %8.1f / %.1f MB  %5.1f%%"
                                     % (done / 1e6, total / 1e6,
                                        100.0 * done / total))
                    sys.stderr.flush()
    sys.stderr.write("\n")
    return out


def download_hls(playlist_url, out_ts, referer=REFERER, max_seconds=0.0):
    text = fetch_text(playlist_url, {"Referer": referer}, timeout=60)

    variants = hls_variants(text, playlist_url)
    if variants:
        url = max(variants)[1]
        print("  master playlist -> picking %s" % url[:80])
        text = fetch_text(url, {"Referer": referer}, timeout=60)
        playlist_url = url

    segs = parse_playlist(text, playlist_url)
    if not segs:
        raise SystemExit("no segments found in the playlist")

    # fMP4 (EXT-X-MAP) or encrypted (EXT-X-KEY) playlists cannot be plain
    # byte-concatenated; hand those to ffmpeg if we have it.
    needs_ffmpeg = "#EXT-X-MAP" in text or "#EXT-X-KEY" in text
    ffmpeg = shutil.which("ffmpeg")
    if needs_ffmpeg:
        if not ffmpeg:
            raise SystemExit("this playlist needs ffmpeg (init segment or encryption)")
        return ffmpeg_pull(playlist_url, out_ts, referer)

    total = sum(d for d, _ in segs)
    print("  %d segments, %.1f min" % (len(segs), total / 60))

    written, done_dur, t0 = 0, 0.0, time.time()
    with open(out_ts, "wb") as fh:
        for i, (dur, url) in enumerate(segs, 1):
            if max_seconds and done_dur >= max_seconds:
                print("  reached --max-seconds, stopping")
                break
            with http_get(url, {"Referer": referer}, timeout=120) as r:
                n = 0
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    n += len(chunk)
            written += n
            done_dur += dur
            print("  seg %2d/%2d  %6.1f MB  total %7.1f MB  %5.1f%%  (%3.0fs)"
                  % (i, len(segs), n / 1e6, written / 1e6,
                     100.0 * done_dur / total, time.time() - t0))
    return out_ts


def ffmpeg_pull(url, out, referer=REFERER):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg is required for this stream")
    cmd = [ffmpeg, "-y", "-loglevel", "warning",
           "-headers", "Referer: %s\r\nUser-Agent: %s\r\n" % (referer, UA),
           "-i", url, "-c", "copy", out]
    print("$ " + " ".join(cmd))
    if subprocess.call(cmd) != 0:
        raise SystemExit("ffmpeg failed")
    return out


def remux_to_mp4(src, dst):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("\nffmpeg not found - keeping the raw .ts (plays in VLC)", file=sys.stderr)
        return None
    cmd = [ffmpeg, "-y", "-loglevel", "warning", "-i", src,
           "-c", "copy", "-movflags", "+faststart", dst]
    print("  remuxing to MP4 ...", file=sys.stderr)
    if subprocess.call(cmd) != 0:
        print("  remux failed - keeping %s" % src, file=sys.stderr)
        return None
    return dst


# --------------------------------------------------------------------- main --

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="play.php URL -> video file")
    ap.add_argument("url", help="the play.php URL")
    ap.add_argument("-o", "--output", help="output file (default: video.mp4)")
    ap.add_argument("--max-seconds", type=float, default=0,
                    help="stop after N seconds of media (preview)")
    ap.add_argument("--info", action="store_true",
                    help="print metadata and the decrypted URL, then exit")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    info = extract(a.url)

    if a.json:
        print(json.dumps(info, indent=2))
    elif a.info:
        print("title   : %s" % info["title"])
        print("drm     : %s" % (info["drm"] or "-"))
        print("url     : %s" % info["media_url"])
    else:
        print("title : %s" % (info["title"] or "(unknown)"))
        print("drm   : %s" % (info["drm"] or "-"))

    if a.info and not a.output:
        return 0

    kind = sniff(info["media_url"], info["origin"])
    print("stream: %s" % kind)

    out = a.output or "video.mp4"
    base, ext = os.path.splitext(out)
    if not ext:
        out = base + ".mp4"
        base, ext = os.path.splitext(out)

    if kind == "hls":
        ts = base + ".ts"
        download_hls(info["media_url"], ts, info["origin"], a.max_seconds)
        made = remux_to_mp4(ts, out) if out.lower().endswith(".mp4") else None
        if made:
            os.remove(ts)
            print("\nsaved: %s" % made)
        else:
            print("\nsaved: %s" % ts)
    else:
        download_progressive(info["media_url"], out, info["origin"])
        print("\nsaved: %s" % out)

    print("      %.1f MB" % (os.path.getsize(out) / 1e6)
          if os.path.exists(out) else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
