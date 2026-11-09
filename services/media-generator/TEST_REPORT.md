# NEAR AI Media Generator — build and test report

**Date:** 2026-11-08 (UTC)  
**Service:** `services/media-generator`  
**Listing:** <https://market.near.ai/a/media-studio-agent>  
**Agent ID:** `01fce9d1-259a-450c-b998-3712c744dfc6`  
**Handle:** `media-studio-agent`  
**Runtime:** self-hosted HTTP, signed webhook + 30 s polling fallback  
**Price:** 25.00 USD per job, 6-hour SLA  
**Public host:** <https://million-tim-involvement-calvin.trycloudflare.com> (Cloudflare quick tunnel)  
**Health:** <https://million-tim-involvement-calvin.trycloudflare.com/healthz>  
**Agent card:** <https://million-tim-involvement-calvin.trycloudflare.com/.well-known/agent-card.json>

---

## 1. Protocol evidence

| Check | Result |
|---|---|
| `GET /healthz` public | **200**, JSON with agent id, handle, providers, queue |
| `GET /.well-known/agent-card.json` public | **200**, A2A card with image/video/voiceover skills |
| Platform webhook test (`webhook.ping`) | **HTTP 200** (540–771 ms) — `webhook_enabled: true` |
| Bad HMAC signature | **401** (rejected) |
| Valid signed `webhook.ping` | **200** |
| Valid signed `hire.created` | **200**, job created and a 5.04 s voiceover produced |
| `POST /v1/hire` through the public tunnel with `X-Test-Key` | **202**, job completed, report + file served over HTTPS |
| A2A `message/send` + `tasks/get` | **completed**, 5.04 s voiceover artifact returned |
| File upload to Agent Market (`POST /v1/files` → PUT → complete) | **real file ID** `7f590d84-3c5c-43b6-829a-642edfec734c`, 56,237 bytes, sha256 match |
| Mock marketplace worker path | **PASS** — `start` → confirmation message → progress → upload → `submit` with file id (recording mock; no charge) |
| Listing status | **live**, category `creative`, runtime `http`, plan 25.00 USD per call, default |

The marketplace worker path is implemented per the official self-hosted agent guide: `POST /webhook` verifies
`HMAC-SHA256("<timestamp>.<raw body>")` with a 300-second window and dedupes on `X-Market-Delivery`;
`GET /v1/agents/me/assignments` polls every 30 s; `start`, `messages`, `progress`, `POST /v1/files`,
and `POST /v1/assignments/{id}/submit` are all called. `hire.changes_requested` runs a targeted revision.

**Paid-hire note:** the owner balance is 0.00 USD / 0.000000 USDC, so a real paid self-hire could not be run
without depositing capital. The signed hire path, real file upload and the full worker call sequence were tested
with a signed webhook and a recording mock; the first real charge will be the buyer's acceptance hire. This is
stated rather than faked.

---

## 2. Sample scenarios

### 2.1 Three square product photos — PASS

- Job: `4183066f-9c42-445f-84c3-b7e39c8dc4fd`
- Brief: `3 square product photos of a ceramic mug on a wooden table`
- Result: **3 images**, each **1080×1080**, JPEG, non-blank, 51–62 KiB.
- Provider/model: **Pollinations.ai — Flux/Sana free image endpoint** (requested `flux`), 768×768 render normalised to 1080×1080.
- Report: <https://million-tim-involvement-calvin.trycloudflare.com/v1/jobs/4183066f-9c42-445f-84c3-b7e39c8dc4fd/report>
- Files: `test-output/images/image-{1,2,3}-1x1.jpg`
- Honest limits: the free endpoint allows **one image per 45 s per IP**, renders a **small pollinations.ai
  attribution mark** that `nologo=true` does not remove without a pollen key, and its 768 px canvas is upscaled.
  The report says so per file.

### 2.2 Eight-second vertical coffee-pour video — PARTIAL (stock fallback, clearly labelled)

- Job: `68cc37b3-f72c-445b-bc73-43517bad93d9`
- Brief: `8-second vertical video of coffee being poured into a cup, stock is fine if AI generation is unavailable`
- Result: **1 video**, **1080×1920**, **8.00 s**, H.264 24 fps, 7.9 MiB.
- Provider/model: **Wikimedia Commons — CC BY 4.0 stock footage, not generated** (source 5.52 s, slowed 1.45× to 8 s, no loop).
- Attribution: `File:Servido tradicional de café etíope.webm` by ProtoplasmaKid, CC BY 4.0.
- Report: <https://million-tim-involvement-calvin.trycloudflare.com/v1/jobs/68cc37b3-f72c-445b-bc73-43517bad93d9/report>
- File: `test-output/video/video-9x16-8s.mp4`

**Why the AI video route failed (exact):**

```
LTX-Video returned `event: error` / `data: null`
```

The public Space is Lightricks LTX-Video Distilled on Hugging Face ZeroGPU. Anonymous ZeroGPU quota is
**2 GPU-minutes per IP per 24 h**; one call reserves **60 s** (75 s for clips over 7 s). The first test call
succeeded and produced a **1.90 s, 512×512** AI clip (`test-output/video/ltx-2s-proof.mp4`), which proves the
provider works at a supported short length. The subsequent 8 s call returned the same opaque error and no
clip. The Space's docs list a **0.3–8.5 s** duration range, so 8 s is supported by the model; the failure is
consistent with the anonymous GPU quota being exhausted, not with an 8 s length limit. The agent did **not**
stretch, loop or freeze-frame the 2 s clip and call it 8 s. It delivered a same-subject, same-length
CC-licensed stock clip and labelled it stock in the file row and report. If the buyer disallows stock, the
agent delivers no video and reports the exact error.

### 2.3 Thirty-second voiceover — PASS

- Job: `50d9995a-a063-447b-84a1-2c2d0064b880`
- Brief: `30-second voiceover, warm female voice`
- Script: 58 words, provided verbatim.
- Result: **30.02 s**, MP3 48 kHz mono, 704 KiB, mean volume −21.9 dB.
- Provider/model: **Microsoft Edge Neural TTS (`edge-tts`) — `en-US-AriaNeural`**, free, no key.
- Timing: speed 0.762× with `atempo`, then `apad`/`atrim` to 30.0 s; within 1 s.
- Report: <https://million-tim-involvement-calvin.trycloudflare.com/v1/jobs/50d9995a-a063-447b-84a1-2c2d0064b880/report>
- File: `test-output/voiceover/voiceover-48k.mp3`
- Word-for-word: the exact script string is passed to the TTS engine; no words are added, removed or paraphrased.
  The report includes the script and its SHA-256.

### 2.4 Celebrity likeness — DECLINED, no provider call

- Job: `3871d8bd-5d4b-47e3-bbeb-0992eadc0fc1`
- Brief: `Image of a well-known celebrity holding the product`
- Result: **declined** (rule `real-person`), no file, no provider call.
- Report: <https://million-tim-involvement-calvin.trycloudflare.com/v1/jobs/3871d8bd-5d4b-47e3-bbeb-0992eadc0fc1/report>

The same rule set declines copyrighted characters, third-party brands/logos without ownership, sexual content,
graphic violence/gore, and deceptive content (fake news, deepfakes, fake documents, impersonation). A mixed brief
is handled part by part; an all-declined brief is declined with a refund.

### 2.5 Targeted revision — PASS

- Base job: `5d649b24-1c06-4416-b0a6-f634e24c0aa7` (3 square images)
- Request: `file 2: make it landscape 16:9, keep the mug and table`
- Result: file 2 regenerated at **1920×1080**; files 1 and 3 kept their original sha256 exactly.
- Report line: “What changed in this version: … Regenerated file(s): 2”.

### 2.6 Reference image — PASS (colour/style hint)

- Job: `570e8ecf-3cff-4708-9066-5a47e70f32ec`
- Brief: `1 square image of a ceramic mug on a wooden table in the style of the reference`, with a public reference image URL.
- Result: 1080×1080 image; the report's provider notes state that the free image route has no pixel-level
  conditioning and that the reference's average colour was used as a style hint, not as an exact likeness.
- This is the honest limit of free reference support: colour guidance, not image-to-image control.

---

## 3. Provider map and limits

| Media | Provider | Model | Cost | Limit stated to the buyer |
|---|---|---|---|---|
| Image | Pollinations.ai | Flux/Sana free endpoint | 0 | 1 image/45 s per IP; 768 px source; small attribution mark |
| Video (AI) | Lightricks LTX-Video Distilled, HF Space | LTX-Video 0.9.x distilled | 0 | 0.3–8.5 s; anonymous ZeroGPU quota 2 min/IP/24 h; may be unavailable |
| Video (fallback) | Wikimedia Commons | CC-licensed stock | 0 | Only when the buyer allows it; labelled stock, never generated |
| Voice | Microsoft Edge Neural TTS | `en-US-AriaNeural`, `Jenny`, `Guy`, `Sonia`, etc. | 0 | No voice cloning; exact script only |

No new accounts, API keys, deposits, KYC, CAPTCHA or duplicate/Sybil accounts were used. The owner's existing
Agent Market account created the listing; the existing human row and `clipper-agent` were not modified.

---

## 4. Reproduce

```bash
cd services/media-generator
./start.sh                     # app + quick tunnel, updates the webhook
./.venv/bin/python provision.py --status
./.venv/bin/python tests/run_samples.py
./.venv/bin/python tests/test_market_mock.py
```

Test outputs and the machine-readable summary are in `test-output/` (`summary.json`, per-scenario `job.json`,
`report.md` and media files). `secrets.json` is mode 600 and is not in the knowledge base.

---

## 5. Honest blockers

1. **AI video quota:** the free LTX/ZeroGPU allowance was exhausted after the first 2 s test. 8 s AI video is
   therefore not demonstrated; the delivered 8 s file is labelled stock. The service reports the exact error and
   retries when the 24-hour quota window resets.
2. **No paid self-hire:** owner balance is 0, so the real charge path is untested; the signed webhook, real file
   upload and the full worker sequence were tested with a recording mock.
3. **Image watermark/resolution:** the free Pollinations tier adds a mark and caps the source at 768 px; the report
   discloses both. Removing them requires a pollen key (capital), which this build does not use.
4. **Semantic subject check:** automated checks verify file integrity, dimensions, aspect, duration, motion and
   non-silence; there is no object detector. The report says this and invites a targeted revision.
