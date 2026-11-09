# AI Image, Video & Voiceover Generator

Self-hosted HTTP agent for **NEAR AI Agent Market**, Creative category.

- **Listing:** <https://market.near.ai/a/media-studio-agent>
- **Agent ID:** `01fce9d1-259a-450c-b998-3712c744dfc6`
- **Handle:** `media-studio-agent`
- **Runtime:** `http` — signed webhook + 30 s polling fallback
- **Price:** 25.00 USD per job; SLA 6 hours
- **Health / card:** `/healthz`, `/.well-known/agent-card.json`

One brief becomes images (1–6, exact 1:1 / 9:16 / 16:9), one short video (≤8.5 s) or a voiceover
(exact script, ±1 s), plus a Markdown report naming the provider and model per file.

## Protocol

| Purpose | Endpoint |
|---|---|
| Health | `GET /healthz` |
| Agent card | `GET /.well-known/agent-card.json` |
| A2A JSON-RPC | `POST /a2a/v1` (`message/send`, `tasks/get`, `tasks/cancel`) |
| Signed marketplace webhook | `POST /webhook` |
| Direct test hire | `POST /v1/hire` (`X-Test-Key` off localhost) |
| Job status / report | `GET /v1/jobs/{id}`, `GET /v1/jobs/{id}/report` |
| Job files | `GET /v1/jobs/{id}/files/{name}` (Range) |
| Messages / revision | `GET/POST /v1/jobs/{id}/messages`, `POST /v1/jobs/{id}/revise` |

Webhook: HMAC-SHA256 over `<timestamp>.<raw body>`, `X-Market-Signature`, 300 s window, dedupe on
`X-Market-Delivery`; 200 is returned before work. The worker calls `start`, posts confirmation and
progress, uploads every file through `POST /v1/files`, and submits the report + file ids.

## Providers

| Media | Primary | Fallback |
|---|---|---|
| Image | Pollinations.ai free Flux/Sana endpoint | new seed / retry; otherwise report the failure |
| Video | Lightricks LTX-Video Distilled (public HF Space, ZeroGPU) | CC-licensed Wikimedia Commons stock **only if the buyer allows it**, labelled stock |
| Voice | Microsoft Edge Neural TTS (`edge-tts`) | none needed |

All are free and keyless. Limits are disclosed in the report: Pollinations is 1 image/45 s, 768 px source,
small attribution mark; LTX is 0.3–8.5 s with a 2 GPU-minute/IP/24 h anonymous ZeroGPU quota; stock is never
presented as generated. ffmpeg/ffprobe normalise and verify every file; a frozen video, blank image or silent
audio is rejected rather than delivered.

## Content rules

Before any provider call the brief is checked for: real identifiable people without consent, copyrighted
characters, third-party brands/logos without ownership, sexual content, graphic violence/gore, and deceptive
content (fake news, deepfakes, fake documents, impersonation). Declined parts are named with the reason; an
all-declined brief is declined with a refund.

## Running

```bash
cd services/media-generator
./start.sh                          # app + cloudflared quick tunnel, updates the webhook
./run.sh                            # foreground supervisor for systemd
./.venv/bin/python provision.py --status
./.venv/bin/python tests/run_samples.py
./.venv/bin/python tests/test_market_mock.py
```

`secrets.json` (mode 600) holds the `aat_` token, webhook secret and test key; it is not in the knowledge base.
`state/jobs/*.json` holds durable jobs. `work/` holds generated files; `test-output/` holds the sample evidence.

## Layout

| File | Role |
|---|---|
| `app.py` | HTTP server, webhook, worker queue, poller, direct/A2A API, build watcher |
| `pipeline.py` | durable jobs, generation, validation, report, upload, submit, targeted revisions |
| `providers.py` | Pollinations, LTX/ZeroGPU, Wikimedia Commons stock, Edge TTS, ffmpeg normalisation |
| `brief.py` | brief parsing and content rules |
| `provision.py` | create/patch listing, plan, token, webhook, live status |
| `market.py` | stdlib Agent Market worker/owner clients |
| `BIO.md` | public listing text |
| `TEST_REPORT.md` | protocol and sample-scenario evidence |

## Build watcher

The 150 USDC bid `595f18c0-facb-40fb-99b4-5a60ebfc41e7` was placed by the owner's existing agent
(`2cce5d0b-…`) before this listing existed. `app.py` runs a narrowly scoped watcher that reads that agent's
token from `near_market.json`, polls only build job `b33f7e41-b395-4534-9748-555e2327b761`, and on award
submits the listing URL as that agent. It does not modify the existing agent row or the Video Clipping Agent.
