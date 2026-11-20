# VIDCLOUD EXTRACTOR — WORKING LOGIC

How the extraction actually works, step by step, and why each step exists.
Companion to `VIDCLOUD_FINDINGS.txt` (raw evidence) and `vidcloud_extract.py` (code).

---

## 1. The one-paragraph version

The `play.php` page never contains a usable video link in plain text. It embeds an
AES-128-CBC ciphertext in a hidden input, `#enc_video_url`, and the page's own
JavaScript decrypts it **in the browser** with a hardcoded key and IV. If we do that
same decryption ourselves, we get a signed Bunny CDN URL that points at a plain MP4
(disguised as `.jpeg`). We then request that URL with two specific headers — a
`Referer` and a deliberately *non*-browser User-Agent — and the CDN serves the file.

So the whole "extraction" is: **fetch page → read one hidden field → AES-decrypt → HTTP GET with two headers.**

---

## 2. Why the obvious approaches fail (the shape of the problem)

Before the logic, the constraints that dictate it. Each of these was tested and
failed, so the design deliberately avoids them.

| Naive approach | Result | Reason |
|---|---|---|
| Use `video_url=...master.mpd` from the query string | `403` / `MissingKey` | It's a decoy. The server ignores it. |
| Download the CloudFront `master.mpd` | `MissingKey` | Private CloudFront distribution, needs a signed cookie the page never mints. |
| `yt-dlp` / generic HLS-DASH tool | nothing | Not a real manifest URL; no standard extractor applies. |
| `grep` the page for `mp4`/`m3u8`/`mpd` | nothing | The URL is encrypted, not present as text. |
| `grep` `video-script.js` for `decrypt`/`AES` | nothing | File is obfuscated (VM-style bytecode + RC4'd string table). |
| Brute-force the key (~1400 combos: MD5/SHA/PBKDF2 of ids, zero IV, etc.) | 0 hits | The key is a **hex string used as raw bytes**, not a passphrase. No KDF applies. |
| Call `/api/...` endpoints | all `404` | `API_BASE` exists in the JS but isn't used for this flow. |
| Just add `Referer` and use a normal browser UA | `403` | The WAF *blocks* real browser UAs (see §7). |

The only thing that worked was **executing the page** and watching the crypto call.

---

## 3. Stage-by-stage logic

### Stage 0 — Input

One `play.php` URL. Of its 10+ query params, only the ObjectIds matter server-side
(`video_id`, `typeId`, `batch_id`, `subject_id`, `topic_id`). Everything else
(`video_name`, `video_img`, `play_type`) is echoed into the HTML for display.
`video_url` is **ignored**.

### Stage 1 — Fetch the page

Plain `GET` with a normal UA. No cookies, no session, no login.

```
GET https://vidcloud.eu.org/play.php?...        -> 200, text/html, 76 KB
```

The response sets a `PHPSESSID`, but it is **not needed** — proven later (§7), the
CDN gate is `Referer`-based, not session-based. So the extractor deliberately does
not maintain a cookie jar. Fewer moving parts.

### Stage 2 — Read the hidden field

Regex the HTML for the hidden input:

```
<input type="hidden" id="enc_video_url" value="zz6m5QqWttpHqD+mR7HRfW7...">
```

Regex used: `id="enc_video_url"[^>]*value="([^"]*)"`

`[^>]*` (not a fixed gap) because attribute order/whitespace can shift. The value is
HTML-unescaped because PHP's `htmlspecialchars` may have turned `&` into `&amp;` —
relevant because the decrypted result is a URL full of `&Policy=`.

Same call is reused for `#enc_drm_type`, `#enc_key_id`, `#enc_key_value`,
`#enc_license_url`, plus metadata (`video_name`, `video_id`, …).

### Stage 3 — AES-128-CBC decrypt

This is the core. Parameters recovered at runtime (§4 explains how):

```
ciphertext = base64_decode(enc_video_url)
key        = bytes.fromhex("5237406b503423784c39216d51322476")   # 16 bytes
iv         = b"T3!nW8$qZ5@rK1#p"                                 # 16 bytes
plaintext  = AES-128-CBC-decrypt(ciphertext, key, iv)  → PKCS#7 unpad
```

Then `decode('utf-8')` and strip trailing `\x00\r\n` (the site pads the plaintext
to a block boundary before encrypting, so the tail can carry filler).

**Critical detail — key handling.** The string `"5237...2476"` is 32 hex chars.
It must be interpreted as **16 raw bytes** via `bytes.fromhex`, *not* as a 32-byte
ASCII passphrase. Getting this wrong is why every brute-force attempt failed. The
resulting 16 bytes happen to be printable ASCII (`R7@kP4#xL9!mQ2$v`), which makes
the mistake look plausible — but the bytes, not the characters, are the key.

**IV derivation (how it was found, and how to re-find it).** The hook showed the key
but the IV lived inside the `[object Object]` config argument. Recovered with CBC
algebra instead:

```
In CBC:  P0 = D(C0) ⊕ IV      =>      IV = D(C0) ⊕ P0
```

`C0` is the first 16 ciphertext bytes. `P0` is the first 16 plaintext bytes, which
are known: every media URL starts with `https://bunny-cd`. Decrypt `C0` with the
key and a zero IV, XOR against that prefix, and the IV falls out as a readable
16-char string: `T3!nW8$qZ5@rK1#p`. That readability is the confirmation it's right.

The same key+IV decrypt `#enc_drm_type` (`4BloTF1TnAw/nwFoOrwx5A==` → `clearkey`).
That's a free self-check: **if `enc_drm_type` doesn't come out as a short lowercase
word, the key or IV is wrong.**

### Stage 4 — Validate the plaintext

Cheap guards before touching the network:

- starts with `https://` (or at minimum `http`)
- contains `&Key-Pair-Id=` (every signed URL does)
- length in the hundreds of bytes, not kilobytes

If these fail → key rotated, go back to §4's re-derivation recipe.

### Stage 5 — Request the media, correctly

This is the part that looks trivial and isn't. Two headers decide everything.

**5a. `Referer` is mandatory.**

```
Referer: https://vidcloud.eu.org/      -> 200 / 206
(no Referer)                           -> 403 AccessDenied
Referer: https://google.com/           -> 403
```

Must be the site's own origin. The `PHPSESSID` cookie is irrelevant — with and
without it, the result tracks the Referer exactly.

**5b. The User-Agent must NOT look like a real browser.**

This is counter-intuitive and the single most likely thing to trip up a
re-implementation:

```
Mozilla/5.0 ... Chrome/131.0.0.0 Safari/537.36   -> 403   BLOCKED
Mozilla/5.0 ... Chrome/120.0 Safari/537.36       -> 403   BLOCKED
curl/8.5.0                                       -> 403   BLOCKED
Mozilla/5.0                                      -> 206   OK
Mozilla/5.0 (Windows NT 10.0; Win64; x64)        -> 206   OK
python-urllib/3.12                               -> 206   OK
(empty)                                          -> 206   OK
```

The WAF denies `/Chrome\/\d+/` and `curl/`. A *correct* browser UA is the wrong
move. The extractor uses `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Safari/537.36`
— realistic-looking but with the `Chrome/<version>` token deliberately removed.
Verified stable: 12 consecutive requests → 12 × 206, zero failures.

**5c. Use `Range` and resume.**

```
Range: bytes=0-1        -> 206, 2 bytes
Range: bytes=0-1048575  -> 206, 1 MiB
```

So a 262 MB pull is resumable. The downloader checks existing file size, sends
`Range: bytes=<size>-`, and appends on `206` / truncates on `200`.

**5d. Always send *a* User-Agent — even for the page.**

Separate from the `Chrome/<version>` rule above: `play.php` itself returns `403`
when the `User-Agent` header is **completely absent**. Verified:

```
(no UA at all)                 -> 403
python-urllib/3.12             -> 200
Mozilla/5.0                    -> 200
curl/8.5.0                     -> 200   (page only - still 403 on the CDN)
```

Python's `urllib` sends **no** `User-Agent` by default, so a bare
`urllib.request.urlopen(play_url)` fails for a reason unrelated to the crypto.
This is a common trap when porting the logic to another language's HTTP client.

### Stage 6 — Write the file

Progressive MP4 → stream bytes straight to disk with a progress meter. No remux,
no re-encode, no ffmpeg. Only if the decrypted URL *itself* ends in `.mpd`/`.m3u8`
would ffmpeg be needed — and that path exists in the code purely as a future-proof
branch; it never triggered for this site.

### Stage 7 — Verify

Checks that confirm a real file rather than an error page:

- size equals `Content-Length` exactly
- first bytes are `\x00\x00\x00 ftypisom...` (not `<?xml` — that's the error body)
- `moov` parses: `avc1` + `mp4a`, duration ≈ 6638 s

---

## 4. How the key was actually recovered (the reusable technique)

Static analysis was a dead end, so the page was **run** and its crypto observed.
This is the transferable method — it works on any client-side-decrypt player.

```
1. Save the page HTML.
2. Rewrite the relative script tag to an absolute origin URL so it still loads:
       src="video-script.js?v=102"
    -> src="https://vidcloud.eu.org/video-script.js?v=102"
3. Inject a hook BEFORE that script runs:
       - wrap CryptoJS.AES.decrypt / .encrypt
       - wrap CryptoJS.enc.Utf8.stringify
       - wrap CryptoJS.PBKDF2
   Each wrapper pushes {args, return} into window.__LOG.
   Use a setInterval poll to wait for window.CryptoJS to appear, because
   crypto-js loads from a CDN and may arrive after the inline hook.
4. After ~15 s, serialize window.__LOG into a DOM node.
5. Run headless with --dump-dom and read it back.
```

Output was four calls; arg1 of the first was the key, and `Utf8.stringify` of the
result was the URL. Two lines of algebra gave the IV. Total time to break: minutes,
versus hours of deobfuscation.

Why not just deobfuscate the JS? It's `javascript-obfuscator` with a bytecode
dispatch loop, a rotated RC4-encrypted string table, and a self-defending
checksum guard that rewrites globals (`Function`, `fetch`, `CryptoJS`, …) via
`Object.defineProperty`. Runtime hooking is strictly cheaper.

---

## 5. Data flow diagram

```
        ┌─────────────────────────────────────────────────────────┐
        │  play.php?...&video_id=...&typeId=...                    │
        │  (video_url=...master.mpd  ← IGNORED / decoy)            │
        └───────────────────────┬─────────────────────────────────┘
                                │  GET (no auth, no cookie needed)
                                ▼
        ┌─────────────────────────────────────────────────────────┐
        │  HTML (76 KB)                                            │
        │   #enc_video_url = "zz6m5Qq..."   (base64, 784 B)        │
        │   #enc_drm_type  = "4BloTF1..."   (base64,  16 B)        │
        │   + video_name / video_id / poster / ...                 │
        └───────────────────────┬─────────────────────────────────┘
                                │  regex id="enc_video_url"...value="(...)"
                                ▼
        ┌─────────────────────────────────────────────────────────┐
        │  AES-128-CBC decrypt                                     │
        │   key = bytes.fromhex("5237...2476")                     │
        │   iv  = b"T3!nW8$qZ5@rK1#p"                              │
        │   PKCS#7 unpad → utf-8 → strip \x00\r\n                  │
        └───────────────────────┬─────────────────────────────────┘
                                ▼
        ┌─────────────────────────────────────────────────────────┐
        │  https://bunny-cdn-.../<78-hex>.jpeg                     │
        │      ?Policy=<b64url json>&Signature=<342ch>&Key-Pair-Id │
        │  (a 262 MB MP4; .jpeg is camouflage; expires in 240 min) │
        └───────────────────────┬─────────────────────────────────┘
                                │  GET + Referer + non-Chrome UA (+ Range)
                                ▼
        ┌─────────────────────────────────────────────────────────┐
        │  206 video/mp4   262,753,748 B                           │
        │  ftyp / mdat / moov  →  avc1 + mp4a, 6638 s              │
        └─────────────────────────────────────────────────────────┘
```

---

## 6. Decisions and their reasons

| Decision | Why |
|---|---|
| Pure stdlib, no pip installs | User constraint (no installs); also makes the tool portable. AES implemented inline. |
| No cookie jar | Proven unnecessary; the gate is `Referer`. Fewer state bugs. |
| Non-Chrome UA | The WAF blocks `/Chrome\/\d+/`; a browser UA actively fails. |
| `Range` + resume | 262 MB; interruptions are likely; `206` is supported. |
| Re-decrypt every run | Signature lives 240 min; a cached URL dies. Never persist it. |
| Validate plaintext before download | A wrong key yields garbage; fail fast with a clear message instead of a mystery 403. |
| `--probe` mode | Lets you tell "URL expired" apart from "code broken" in one request. |
| `--decrypt` mode | Isolates the crypto stage for debugging, independent of network. |

---

## 7. Known failure modes and what each means

| Symptom | Cause | Fix |
|---|---|---|
| `403 AccessDenied` on media | Missing/wrong `Referer` | Set `Referer: https://vidcloud.eu.org/` |
| `403` with an empty User-Agent | Cloudflare on `play.php` blocks UA-less requests | Always send *some* UA (even `Mozilla/5.0`) |
| `403` with a normal browser UA | WAF blocks `Chrome/<n>` | Strip the `Chrome/…` token from the UA |
| `403` after some hours | Signature expired | Re-fetch the page, re-decrypt |
| `MissingKey` (XML) | You fetched the decoy CloudFront `.mpd` | Ignore `video_url`; use `#enc_video_url` |
| Garbage plaintext | Key/IV rotated | Re-run the hook recipe in §4 |
| `enc_drm_type` not `clearkey` | Key/IV wrong | Use it as a self-test |
| File starts with `<?xml` | You saved an error body, not media | Check status/headers first |
| Player won't start but file is valid | `moov` is after `mdat` | Expected; range-request the tail or download fully |

---

## 8. Why it looks like this (author's design, inferred)

- **AES in the browser, key shipped in the JS** — obfuscation-as-deterrent. It
  stops casual scraping and makes the URL absent from the HTML, but a static key
  in client-side code is recoverable by definition. This is the central weakness.
- **`.jpeg` extension on an MP4** — so bucket listings and CDN logs don't advertise
  video.
- **240-minute signatures** — short-lived tickets force clients back through the
  page, keeping `play.php` in the request path (and letting it be analytics-gated).
- **Referer gate** — blocks hotlinking from other sites.
- **WAF that blocks browser UAs** — anti-automation aimed at headless/automated
  browsers; ironically it lets a plain `Mozilla/5.0` through.
- **Hostname allowlist in the script** (`studytalk.cc`, `vidcloud.eu.org`,
  `stream.studytalk.cc`, `localhost`) — anti-clone: the player blanks the page if
  the hostname isn't recognised, so a copied `video-script.js` won't run elsewhere.
- **`clearkey` DRM label with empty key fields** — cosmetic; the media is plain.

---

## 9. Cost / performance notes

- Page fetch: ~76 KB, one request.
- Decryption: 784 bytes, 49 AES blocks, sub-millisecond in pure Python.
- Media: single HTTP GET (or ranged). 262 MB at typical CDN speeds.
- Total wall time for a full extract+download: network-bound; the crypto is free.
- The pure-Python AES was verified byte-identical to openssl on 60 random vectors
  (AES-128-CBC) plus one AES-256-CBC case, so a future 256-bit key rotation needs
  no code change.

---

## 10. Re-derivation recipe (if the key rotates)

1. Fetch the page; grab the current `#enc_video_url`.
2. Try the known key/IV. If `#enc_drm_type` doesn't yield `clearkey`, it rotated.
3. Re-run the hook from §4 — wrap `CryptoJS.AES.decrypt`, read `arg1` (new key).
4. Recover the IV with `IV = D(C0, key, 0) ⊕ "https://bunny-cd"`.
5. Expect the same style: 32-char hex key (16 raw bytes), 16-char printable IV.
6. Verify: decrypt `#enc_drm_type` → `clearkey`; decrypt the URL → ends with
   `&Key-Pair-Id=`.

---

## 11. Minimal reference implementation

The whole method, condensed (no error handling, for clarity):

```python
import base64, re, subprocess, urllib.request

KEY = bytes.fromhex("5237406b503423784c39216d51322476")
IV  = b"T3!nW8$qZ5@rK1#p"

# NOTE: play.php 403s on an EMPTY User-Agent, so always send one.
req = urllib.request.Request(PLAY_URL, headers={"User-Agent": "Mozilla/5.0"})
page = urllib.request.urlopen(req).read().decode()
blob = re.search(r'id="enc_video_url"[^>]*value="([^"]+)"', page).group(1)

ct = base64.b64decode(blob)
url = subprocess.run(
    ["openssl", "enc", "-d", "-aes-128-cbc", "-K", KEY.hex(), "-iv", IV.hex()],
    input=ct, capture_output=True).stdout.decode("utf-8", "replace").strip()

req = urllib.request.Request(url, headers={
    "Referer": "https://vidcloud.eu.org/",
    "User-Agent": "Mozilla/5.0",          # NOT a Chrome/… UA
})
open("out.mp4", "wb").write(urllib.request.urlopen(req).read())
```

That's the entire mechanism: **one hidden field, one AES decrypt, two headers.**

---

## 12. Files

| File | Purpose |
|---|---|
| `vidcloud_extract.py` | The working extractor (pure stdlib, AES inline). |
| `VIDCLOUD_FINDINGS.txt` | Evidence: every observation, measurement, dead end. |
| `VIDCLOUD_LOGIC.md` | This document: the working logic and why. |
