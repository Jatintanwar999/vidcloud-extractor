#!/usr/bin/env python3
"""
vidcloud / play.php video extractor
===================================

The player page (https://vidcloud.eu.org/play.php?...) does NOT contain a
playable URL in plain text. It ships an encrypted blob:

    <input type="hidden" id="enc_video_url" value="<base64>">

and the page's JS (video-script.js) decrypts it in the browser with
CryptoJS.AES.decrypt(blob, key) using a STATIC key + IV:

    key = "5237406b503423784c39216d51322476"  -> bytes.fromhex(...) = 16 raw bytes
    iv  = "T3!nW8$qZ5@rK1#p"                  -> 16 ASCII bytes

i.e. AES-128-CBC. The same key/IV also decrypts #enc_drm_type (-> "clearkey")
and any other #enc_* field the page adds.

So the extraction logic is:

    1. GET the play.php URL
    2. read #enc_video_url (+ #enc_drm_type / #enc_key_id / #enc_key_value ...)
    3. AES-128-CBC decrypt -> the real media URL (signed CDN link)
    4. download it (progressive / HLS / DASH), or just print it

Usage
-----
    python3 vidcloud_extract.py "<play.php url>"             # print media url + info
    python3 vidcloud_extract.py "<play.php url>" -o out.mp4  # download (needs ffmpeg)
    python3 vidcloud_extract.py "<play.php url>" --probe     # check if the CDN serves it
    python3 vidcloud_extract.py --decrypt "<base64 blob>"    # decrypt a blob directly

Pure stdlib: no pip installs needed (AES is implemented below).
ffmpeg is only used if it is already on PATH, to remux HLS/DASH.
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# NOTE: the CDN's WAF returns 403 for user agents that contain a "Chrome/<version>"
# token, so keep this UA free of one.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Safari/537.36")

KEY_HEX = "5237406b503423784c39216d51322476"
IV_STR = "T3!nW8$qZ5@rK1#p"

# --------------------------------------------------------------- AES-128 ----
# (tiny, dependency-free implementation - only what is needed to decrypt)


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

    return sbox, inv, [mul(i, 9) for i in range(256)], [mul(i, 11) for i in range(256)], \
        [mul(i, 13) for i in range(256)], [mul(i, 14) for i in range(256)]


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
    # state is column-major: s[col*4 + row]; row r is rotated right by r
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
    """Decrypt one #enc_* value exactly like CryptoJS.AES.decrypt() does."""
    raw = base64.b64decode(b64_blob.strip())
    pt = aes_cbc_decrypt(raw, bytes.fromhex(key_hex), iv_str.encode())
    return _unpad(pt).decode("utf-8", "replace").rstrip("\x00\r\n ")


# ------------------------------------------------------------------ http ----


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_text(url, headers=None, timeout=30):
    with http_get(url, headers, timeout) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc, "replace")


# ----------------------------------------------------------------- parse ----


def page_field(html, field, default=""):
    m = re.search(r'id="%s"[^>]*value="([^"]*)"' % re.escape(field), html)
    if not m:
        return default
    import html as _html
    return _html.unescape(m.group(1))


def extract(play_url):
    html = fetch_text(play_url, {"Referer": "https://vidcloud.eu.org/"})
    origin = "{0.scheme}://{0.netloc}/".format(urllib.parse.urlsplit(play_url))
    info = {
        "page": play_url,
        "title": page_field(html, "video_name"),
        "video_id": page_field(html, "video_id"),
        "batch_id": page_field(html, "batch_id"),
        "subject_id": page_field(html, "subject_id"),
        "topic_id": page_field(html, "topic_id"),
        "play_type": page_field(html, "play_type"),
        "poster": page_field(html, "video_img"),
        "key_id": page_field(html, "enc_key_id"),
        "key_value": page_field(html, "enc_key_value"),
        "license_url": page_field(html, "enc_license_url"),
        "origin": origin,
    }
    enc_url = page_field(html, "enc_video_url")
    enc_drm = page_field(html, "enc_drm_type")
    if not enc_url:
        raise SystemExit("no #enc_video_url on the page - layout changed?")
    info["media_url"] = decrypt_blob(enc_url)
    info["drm"] = decrypt_blob(enc_drm) if enc_drm else ""
    return info


# -------------------------------------------------------------- download ----


def probe(url, referer="https://vidcloud.eu.org/"):
    """Is the signed CDN link usable?  (it is Referer-gated and time-limited)"""
    try:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Referer", referer)
        req.add_header("Range", "bytes=0-1")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("Content-Type"), r.headers.get("Content-Length")
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace").strip()
        return e.code, e.headers.get("Content-Type"), body
    except Exception as e:  # noqa: BLE001
        return None, None, str(e)


def download(url, out, referer="https://vidcloud.eu.org/"):
    """Stream the file with a Referer header (the CDN 403s without it)."""
    hdrs = {"Referer": referer}

    # HLS / DASH need a real muxer
    if url.split("?")[0].lower().endswith((".mpd", ".m3u8")):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise SystemExit("this link is HLS/DASH - ffmpeg is required to mux it")
        cmd = [ffmpeg, "-y", "-loglevel", "warning",
               "-headers", f"Referer: {referer}\r\nUser-Agent: {UA}\r\n",
               "-i", url, "-c", "copy", out]
        print("$", " ".join(cmd), file=sys.stderr)
        return subprocess.call(cmd)

    # plain progressive file: resumable range download
    done = os.path.getsize(out) if os.path.exists(out) else 0
    if done:
        hdrs["Range"] = f"bytes={done}-"
    with http_get(url, hdrs, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0) + done
        mode = "ab" if r.status == 206 else "wb"
        if mode == "wb":
            done = 0
        with open(out, mode) as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = 100.0 * done / total
                    sys.stderr.write(f"\r  {done/1e6:8.1f} / {total/1e6:.1f} MB  {pct:5.1f}%")
                    sys.stderr.flush()
    sys.stderr.write("\n")
    return 0


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


# ------------------------------------------------------------------ main ----


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Extract the real video URL from a vidcloud play.php page")
    ap.add_argument("url", nargs="?", help="the play.php URL")
    ap.add_argument("--decrypt", metavar="B64",
                    help="decrypt an enc_video_url blob and exit")
    ap.add_argument("-o", "--output", help="download to this file")
    ap.add_argument("--probe", action="store_true",
                    help="check whether the CDN still serves the URL")
    ap.add_argument("--json", action="store_true", help="print everything as JSON")
    ap.add_argument("--m3u8", action="store_true",
                    help="if it is an HLS master playlist, list the variants")
    a = ap.parse_args(argv)

    if a.decrypt:
        print(decrypt_blob(a.decrypt))
        return 0
    if not a.url:
        ap.error("give a play.php URL (or --decrypt B64)")

    info = extract(a.url)

    if a.json:
        print(json.dumps(info, indent=2))
    else:
        print(f"title     : {info['title']}")
        print(f"video_id  : {info['video_id']}")
        print(f"drm       : {info['drm'] or '-'}")
        if info["key_id"]:
            print(f"key id    : {info['key_id']}")
            print(f"key value : {info['key_value']}")
        print(f"media url : {info['media_url']}")

    if a.probe or a.output or a.m3u8:
        st, ct, extra = probe(info["media_url"], info["origin"])
        print(f"\nprobe     : HTTP {st}  {ct}  {extra}", file=sys.stderr)

    if a.m3u8:
        try:
            for bw, u in sorted(hls_variants(fetch_text(info["media_url"]),
                                             info["media_url"]), reverse=True):
                print(f"{bw:>10}  {u}")
        except Exception as e:  # noqa: BLE001
            print("not an m3u8:", e, file=sys.stderr)

    if a.output:
        rc = download(info["media_url"], a.output, info["origin"])
        print(f"\nsaved to {a.output} (exit {rc})", file=sys.stderr)
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
