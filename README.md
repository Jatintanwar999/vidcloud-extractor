# vidcloud-extractor

Extractor for `https://vidcloud.eu.org/play.php` lecture pages.

The page never exposes a playable link. It embeds an AES-128-CBC ciphertext in a
hidden input and decrypts it **in the browser** using a key and IV that are
hardcoded in the site's JavaScript. This tool performs the same decryption
offline, then downloads the result.

Pure Python standard library — no pip installs, no ffmpeg, no yt-dlp.

---

## The mechanism

```
#enc_video_url (base64 hidden input)
  -> AES-128-CBC decrypt
  -> key = bytes.fromhex("5237406b503423784c39216d51322476")   # 16 raw bytes, NOT a passphrase
  -> iv  = b"T3!nW8$qZ5@rK1#p"
  -> signed Bunny CDN URL (a real MP4 wearing a .jpeg extension)
```

The same key/IV decrypt `#enc_drm_type` to `clearkey`. That doubles as a
self-test: if it does not come out as a short lowercase word, the key or IV has
rotated.

The `video_url=...master.mpd` query parameter is a **decoy**. The server ignores
it, and that CloudFront `.mpd` is a private distribution that returns `MissingKey`
without a signed cookie the page never mints.

---

## Two header rules, two different hosts

This is the part that breaks most re-implementations.

| Host | Requirement |
|---|---|
| `play.php` (vidcloud.eu.org) | Needs **some** non-empty `User-Agent`. Empty UA → `403`. No Referer, no cookie. |
| media CDN (`bunny-cdn-*.cc`) | Needs `Referer: https://vidcloud.eu.org/` **AND** a UA with **no** `Chrome/<version>` token. |

Counter-intuitive: a *correct* browser UA is **blocked** by the CDN's WAF. Plain
`Mozilla/5.0` works. `curl/` is blocked on the CDN but fine on `play.php`.

---

## Usage

```bash
# print the real media URL plus metadata
python3 vidcloud_extract.py "<play.php url>"

# download (resumable, progress meter)
python3 vidcloud_extract.py "<play.php url>" -o lecture.mp4

# is the signed link still alive?
python3 vidcloud_extract.py "<play.php url>" --probe

# everything as JSON
python3 vidcloud_extract.py "<play.php url>" --json

# decrypt any #enc_* blob directly, no network
python3 vidcloud_extract.py --decrypt "<base64 blob>"
```

The signed URL expires **240 minutes** after the page load, so always decrypt
fresh rather than caching a URL.

---

## Verified output

- Size: `262,753,748` bytes (exact `Content-Length`)
- Boxes: `ftyp/isom` + `mdat` + `moov`
- Tracks: `avc1` (H.264) + `mp4a` (AAC)
- Duration: `6,638.0` s (1 h 50 m 38 s)
- `moov` sits **after** `mdat` (non-faststart), so a partial download will show
  no `moov` — that is the layout, not corruption.

---

## Documentation

| File | Contents |
|---|---|
| [`docs/VIDCLOUD_FINDINGS.txt`](docs/VIDCLOUD_FINDINGS.txt) | Raw evidence: every observation, measurement, dead end, and the recovered crypto parameters. |
| [`docs/VIDCLOUD_LOGIC.md`](docs/VIDCLOUD_LOGIC.md) | The working algorithm, stage by stage, with reasoning and a minimal reference implementation. |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | Condensed context note for resuming work in a fresh session. |

Start with `VIDCLOUD_LOGIC.md` if you want to understand *how*, and
`VIDCLOUD_FINDINGS.txt` if you want to verify *why each step is what it is*.

---

## If the key rotates

1. Fetch the page and grab the current `#enc_video_url`.
2. Test the known key/IV against `#enc_drm_type`; if it is not `clearkey`, it rotated.
3. Run the page in a headless browser with `CryptoJS.AES.decrypt` wrapped before
   `video-script.js` loads, and read `arg1` for the new key.
4. Recover the IV algebraically:
   `IV = D(C0, key, IV=0) XOR "https://bunny-cd"`
   where `C0` is the first ciphertext block and the XOR operand is the known
   plaintext prefix.
5. Expect the same style: a 32-character hex key and a 16-character printable IV.

Static deobfuscation of `video-script.js` is not worth attempting — it uses a
VM-style bytecode dispatch loop, an RC4-encrypted rotating string table, and an
anti-tamper checksum guard. Runtime hooking is far cheaper.

---

## Notes

- The AES implementation is inline and was verified byte-identical to `openssl`
  across 60 random AES-128-CBC vectors, plus one AES-256-CBC case, so a future
  key rotation to 256-bit needs no code change.
- No secrets, cookies, or credentials are stored in this repository.
- `play.php` pages are third-party mirrors of paid course content. This code is
  published for study of the client-side obfuscation technique. What you do with
  the output — personal offline viewing versus redistribution — is a legal
  distinction, and the latter is not what this is for.
