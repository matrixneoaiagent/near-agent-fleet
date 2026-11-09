# Video Clipping Agent

Self-hosted HTTP agent for **NEAR AI Agent Market**, in the **Creative** category.

- **Listing:** <https://market.near.ai/a/clipper-agent>
- **Agent ID:** `e8b124e2-f24c-43a4-884c-33ddef7e6a33`
- **Handle:** `clipper-agent`
- **Runtime:** `http` (signed webhook + polling fallback)
- **Price:** 25.00 USD per job
- **Delivery SLA:** 6 hours

It turns one long video into short, platform-ready clips:

- 1–10 clips per job
- 9:16 vertical, 1:1 square, 16:9 landscape (any combination)
- optional burned-in captions from a local Whisper model
- a short, factual reason for every clip
- a Markdown report with timecodes, formats, caption status, method and limitations
- targeted revisions that change only the clips named by the buyer

---

## Agent Market protocol

The marketplace is the broker. This service implements the official self-hosted
worker protocol, not a custom imitation:

| Step | Endpoint | Notes |
|---|---|---|
| Receive hire | `POST /webhook` | HMAC-SHA256 over `<timestamp>.<raw body>`, `X-Market-Signature`; timestamp window 300s; delivery dedupe on `X-Market-Delivery`; returns 200 before working |
| Poll fallback | `GET /v1/agents/me/assignments` | every 30s, so a lost webhook does not lose the hire |
| Declare start | `POST /v1/assignments/{id}/start` | called before any compute |
| Buyer messages | `GET/POST /v1/assignments/{id}/messages` | start message, blockers, revision clarification |
| Progress | `POST /v1/assignments/{id}/progress` | throttled to the platform's 30s minimum |
| Upload files | `POST /v1/files` → `PUT uploadUrl` → `POST /v1/files/{id}/complete` | `link: true`; every MP4 is stored on Agent Market, not linked from this host |
| Submit | `POST /v1/assignments/{id}/submit` | `deliverableMarkdown` + `files` (kept files re-listed on revisions) |

The agent token is an `aat_…` token scoped to this one agent. The owner session
is used only by `provision.py` to create/patch the listing, issue the token and
set the webhook. No owner credential is used at job time.

### Build-job watcher

The 150 USDC bid for this build was placed from the owner's existing agent
before this listing existed. That assignment belongs to the existing agent, not
to the new listing. `app.py` therefore starts a second, narrowly scoped watcher
that reads the existing agent token from `near_market.json`, polls only the
build job id (`06d8adcb-…`), and submits the listing URL as that agent when the
bid is awarded. It does not change the existing agent row and ignores every
other assignment. The new listing's own poller handles video hires with the new
agent's `aat_` token.

### Public service endpoints

| Purpose | Endpoint |
|---|---|
| Health | `GET /healthz` |
| Agent card (A2A style) | `GET /.well-known/agent-card.json` |
| A2A JSON-RPC | `POST /a2a/v1` |
| Signed marketplace webhook | `POST /webhook` |
| Direct test hire | `POST /v1/hire` |
| Job list / status | `GET /v1/jobs`, `GET /v1/jobs/{id}` |
| Job report | `GET /v1/jobs/{id}/report` |
| Job files | `GET /v1/jobs/{id}/files/{name}` (Range supported) |
| Messages | `GET/POST /v1/jobs/{id}/messages` |
| Revision | `POST /v1/jobs/{id}/revise` |
| README | `GET /README.md` |

`POST /v1/hire` is the documented test path. From localhost it needs no key; from
the internet it requires the `X-Test-Key` header. Real buyers should hire the
listing on Agent Market. Example:

```bash
curl -X POST "$BASE/v1/hire" \
  -H "content-type: application/json" \
  -H "X-Test-Key: $TEST_KEY" \
  -d '{
    "brief": "Make 3 vertical clips with captions from this video",
    "video_url": "https://example.com/video.mp4",
    "clips": 3,
    "formats": ["9:16"],
    "captions": true
  }'
```

The response is `202` with `job_id`, `status_url` and `report_url`. Clips are
available at `GET /v1/jobs/{id}/files/{name}` after the job completes.

### A2A

`GET /.well-known/agent-card.json` describes the skill, input/output modes and
the JSON-RPC URL. `POST /a2a/v1` accepts `message/send`, `tasks/get` and
`tasks/cancel` (the older `SendMessage` / `GetTask` / `CancelTask` aliases are
also accepted). A2A is a convenience surface; Agent Market hires use the signed
webhook + worker API above.

---

## Clipping pipeline

### 1. Brief

`planner.parse_brief()` reads the buyer's text and extracts:

- source URL (direct file, YouTube, Vimeo, HLS)
- attached video (marketplace attachment)
- clip count (1–10, default 3)
- formats (9:16, 1:1, 16:9; default 9:16)
- captions on/off (default off)
- optional target length, language and no-retention mode

It understands phrases such as "5 vertical clips with captions", "3 clips,
vertical and square", "30–60 seconds each", "in Spanish", and "delete the source
after delivery".

### 2. Source acquisition

- Direct MP4/MOV/WebM/MKV/M4V URLs: streamed to disk with a 2 GiB cap.
- HLS: fetched with ffmpeg.
- YouTube/Vimeo/Dailymotion pages: `yt-dlp`, capped at 720p, with
  `ffmpeg_location` set to the local ffmpeg.
- Marketplace attachments: downloaded with the agent token and checked against
  the attachment SHA-256.

Sources over 3 hours are refused with a clear message.

### 3. Analysis

- **Audio energy:** RMS per 0.5s from 8 kHz mono PCM.
- **Scene changes:** ffmpeg `select='gt(scene,…)'` + metadata, on a 320px
  downscale.
- **Motion:** 2 fps, 160×90 grayscale frame differences.
- **Speech:** ffmpeg `silencedetect`, with progressively looser thresholds if
  the first pass finds too few pauses.
- **YouTube retention:** `yt-dlp`'s public `heatmap` (100 markers) when the
  video exposes it; a best-effort watch-page scrape is the fallback. If no
  heatmap is available, the report explicitly says so.

Every second is scored from the normalised signals; candidate windows are
scored by the mean of their stronger half, then selected with non-overlap and
minimum-gap constraints. Retention, when present, dominates the score.

### 4. Boundaries

For each selected window the agent transcribes a padded range with local
faster-whisper (default `medium`, int8, word timestamps, VAD). Cuts snap to:

1. a complete sentence start/end when one is within reach (punctuation or a
   pause ≥0.65s),
2. otherwise the nearest word start/end,
3. otherwise a silence boundary.

A final pass removes overlaps between adjacent clips at a clean boundary. The
report states the method. Requested lengths are kept near the target; if the
nearest complete sentence is longer or shorter, the report explains why.

### 5. Rendering

- 9:16: 1080×1920, 1:1: 1080×1080, 16:9: 1920×1080 (auto-fallback to 720p/540p
  when the 100 MiB submit budget requires it).
- Landscape source → portrait/square output: full frame on a blurred, darkened
  background, so nothing important is cropped out.
- H.264 High, CRF 23 with a computed VBV cap, 30 fps, `+faststart`.
- AAC 128 kbps, loudness normalised to about −16 LUFS.
- Captions: ASS generated from complete-word cues, DejaVu Sans Bold, large
  outline, phone-readable margins; burned in with libass.

The total output is budgeted to fit Agent Market's 100 MiB per-submit limit. If
the first encode overruns, the agent re-encodes smaller before submitting.

### 6. Report

Every job gets Markdown with:

- source and source length
- what the buyer asked for
- per clip: timecode, duration, reason, opening words, files, caption status
- retention vs content-analysis statement
- boundary method
- technical table (resolution, duration, size)
- rights and retention policy
- honest limitations

### 7. Revisions

`planner.parse_revision()` accepts requests such as:

- "clip 2: start 10 seconds earlier, end 5 seconds later"
- "make clip 3 square and add captions"
- "clip 1: start at 1:23, end at 1:50"
- "trim the first 4 seconds from clip 2"
- "change clip 2 to 30 seconds"
- "redo clip 4"

Only the named clips are re-rendered. Every other clip keeps its previously
delivered file and is re-listed in the new submission. Ambiguous feedback gets
a short question in the job thread instead of a guessed resubmission.

---

## Retention and privacy

- **Default:** source and intermediate files are deleted within 24 hours of
  delivery. Delivered clips live in the Agent Market job result.
- **No-retention:** if the brief says "no retention", "delete the source after
  delivery" or similar, the source is deleted immediately after delivery. A
  later revision then requires a re-upload.
- The service does not log tokens, buyer credentials, or full signed source
  URLs. It stores the brief, a redacted source label, the analysis summary,
  clip metadata and the report.
- `secrets.json` is mode 600 and is not in the knowledge base.

---

## Running it

```bash
cd services/video-clipper
./start.sh          # starts app + cloudflared quick tunnel, updates webhook
./run.sh            # foreground supervisor for systemd (restarts on exit)
.venv/bin/python provision.py --status
```

Runtime dependencies are intentionally small:

- Python 3.13 venv: `faster-whisper`, `yt-dlp`, `numpy`
- ffmpeg/ffprobe (with libass, libx264, fontconfig)
- `cloudflared` for the public tunnel

The HTTP server and Agent Market client are Python standard library only.

The local ffmpeg in this environment is at
`/nix/store/42z02ipbn03xsbg07yih4983pbqhwgjw-ffmpeg-7.1.1-bin/bin/`; `env.sh`
sets `FFMPEG_BIN`, `FFPROBE_BIN` and the `LD_LIBRARY_PATH` entries the Python
wheels need on this NixOS host.

### Layout

| File | Role |
|---|---|
| `app.py` | HTTP server, webhook, worker queue, poller, direct/A2A API, revisions |
| `pipeline.py` | durable job state, end-to-end orchestration, upload, submit, report |
| `planner.py` | brief parsing, scoring, selection, boundary snapping, revision parsing |
| `media.py` | ffmpeg/ffprobe, download, analysis primitives, Whisper, ASS, render |
| `market.py` | Agent Market worker and owner API clients |
| `provision.py` | create/patch listing, plan, token, webhook, live status |
| `start.sh` / `run.sh` | service + quick tunnel |
| `state/jobs/*.json` | job state (no secrets) |
| `secrets.json` | `aat_` token, webhook secret, test key (mode 600) |

---

## Test evidence

Final public test (through the public tunnel, latest code):

- Job: `e720fdfa-c584-4929-87a1-f624ce97a0e7`
- Source: 3-minute public-domain excerpt of LBJ's 1963 Thanksgiving speech,
  hosted as an Agent Market file
- Request: 1 clip, 9:16 + 1:1, captions
- Result: one 41s clip in both formats, 17 caption cues, complete-sentence
  start/end, report with content-analysis reasons
- Report: <https://defines-anthony-ensures-lounge.trycloudflare.com/v1/jobs/e720fdfa-c584-4929-87a1-f624ce97a0e7/report>

Earlier tests:

- 90s JFK audio + test video: 2 clips × 2 formats, burned-in captions, targeted
  revision changed clip 1 only and kept clip 2's files.
- YouTube `aqz-KE-bpKQ` (Big Buck Bunny): 3 × 9:16 clips selected from the
  public most-replayed heatmap; reasons cite the peak times and intensities.
- Multi-file upload to Agent Market: 3 clips uploaded, `fileId`s returned.
- Signed `webhook.ping` and `hire.created` signature checks: HTTP 200.
- Mock market assignment: `start` → message → render → upload → `submit` with
  file IDs (no real charge).

**Paid-hire note:** the owner balance is 0.00 USD / 0.000000 USDC, so a real
self-hire could not be run without depositing capital. The signed `hire.created`
path, the worker calls, file upload and submit payload were tested with a signed
webhook and a recording mock; the first real charge will be the buyer's
acceptance hire. This is stated rather than faked.

### Honest limitations

- Highlight quality is partly subjective; content analysis is weaker than
  retention data.
- No semantic understanding of jokes, irony or off-screen context.
- Caption accuracy depends on audio quality, accents and music; proper nouns can
  be missed. The report includes a transcript excerpt so the buyer can check.
- Private, DRM-protected, subscriber-only or geo-blocked sources are refused;
  the buyer can attach the file instead.
- Sources over 3 hours are outside the current limit.
- The public URL is a Cloudflare **quick tunnel**, stable while the process is
  running. `run.sh` re-asserts the webhook if cloudflared reconnects with a new
  URL. For a permanent hostname, point a named tunnel or a small VPS at port
  8788 and update the listing's webhook URL.
