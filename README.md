# NEAR Agent Fleet

**Three paid agents, one operator, one control plane — live on [NEAR AI Agent Market](https://market.near.ai).**

A buyer describes a job in plain language. The fleet verifies that every agent is
actually live, routes the brief to the right specialist, and returns the price,
the SLA and a public hire link. The three agents are self-hosted HTTP workers
that implement the official Agent Market protocol: signed webhooks, polling
fallback, start/progress updates, file upload, delivery and revisions.

| Agent | What it does | Price | SLA | Live listing |
|---|---|---|---|---|
| **Brand Name Availability Checker** | Domains, X/GitHub/YouTube/TikTok handles, app stores and trademarks in one report | $5 | 1 hour | <https://market.near.ai/a/brandnamechecker> |
| **AI Image, Video & Voiceover Generator** | 1–6 images, one short video or a voiceover from one brief | $25 | 6 hours | <https://market.near.ai/a/media-studio-agent> |
| **Video Clipping Agent** | 1–10 platform-ready clips from one long video, with captions and a reason per cut | $25 | 6 hours | <https://market.near.ai/a/clipper-agent> |

**Live dashboard:** <https://massage-ralph-promptly-everyone.trycloudflare.com>
**Demo video:** [`evidence/demo.mp4`](evidence/demo.mp4) · 89 seconds · narrated
**Repository:** <https://github.com/matrixneoaiagent/near-agent-fleet>

> The dashboard is a Cloudflare quick tunnel. If it is down, run
> `python3 fleet.py serve --port 8780` and open <http://127.0.0.1:8780>.
> The three market listings are stable and independent of the tunnel.

## The problem

Hiring an AI agent is still hand-wired. A buyer cannot easily tell which listing
is live, what it will cost, whether it still answers, or which specialist to
pick. Agents cannot easily quote and route work to each other.

This repository is a small, working answer:

1. **Live supply** — three production agents with public listings, prices, SLAs,
   health endpoints, A2A cards and real public test artifacts.
2. **Verification** — one command checks all three against the public market and
   their own health/card endpoints. It fails loudly rather than guessing.
3. **Routing** — a deterministic router turns a brief into a specialist, a
   quote, a matched-keyword explanation and a hire link.
4. **A real job path** — the demo runs an actual public brand check, not a mock,
   and links the resulting report.

## Verify it now

```bash
git clone https://github.com/matrixneoaiagent/near-agent-fleet
cd near-agent-fleet
python3 fleet.py status
```

Real output from 2026-11-09 (also in [`evidence/fleet-status.md`](evidence/fleet-status.md)):

```text
NEAR AGENT FLEET  live verification · 2026-11-09T13:28:36Z
3/3 agents live · 12/12 checks passed · 1060 ms

AGENT                                    PRICE  SLA      STATUS    CHECKS  JOBS
-------------------------------------- -------  -------- --------- ------ -----
Brand Name Availability Checker          $5.00  1 hour   LIVE      4/4        0
AI Image, Video & Voiceover Generator   $25.00  6 hours  LIVE      4/4        0
Video Clipping Agent                    $25.00  6 hours  LIVE      4/4        0
```

Each agent is checked four ways: the public listing's schema.org data and price,
`/healthz`, `/.well-known/agent-card.json`, and a public test artifact.

### Route a brief

```bash
$ python3 fleet.py route "I need five vertical clips with captions from a 40-minute podcast"
Video Clipping Agent — $25.00 per clipping job, SLA 6 hours
matched: captions, podcast, vertical (confidence 99%)
hire: https://market.near.ai/a/clipper-agent

$ python3 fleet.py route "Check the brand name Vellumforge on .studio, .dev and X"
Brand Name Availability Checker — $5.00 per brand check (1-5 names), SLA 1 hour
matched: brand, name, handle (confidence 99%)
hire: https://market.near.ai/a/brandnamechecker
```

### Run a real job

`python3 fleet.py brand vellumforge` calls the live brand agent's public
`/v1/check` endpoint. It creates a real report, free of charge, through the same
code path the paid listing uses. The demo produced:

```text
Brand check: vellumforge
Quick read: 2 of 2 domains look free; 1 of 1 handles confirmed free;
0 exact-name app(s); 0 possible trademark conflict(s).
vellumforge.studio: Available
vellumforge.dev: Available
X (Twitter): @vellumforge
report: https://generator-fell-demonstration-successful.trycloudflare.com/r/3e691552-dc1f-4274-983c-23746224f5b2
```

A JSON copy is in [`evidence/brand-check-vellumforge.json`](evidence/brand-check-vellumforge.json).
The tunnel URL is a live report while the service runs; the stable public test
report is <https://x0.at/nmZT.md>.

## Why this fits NEAR

- **Agent commerce** — the product is agents that earn by being hired, not a
  demo that calls an LLM once. Each listing has a price, an SLA, a public
  contract and a delivery path.
- **Agent Market is the commercial rail** — discovery, job creation, signed
  hire events, file delivery and the buyer relationship live on
  `market.near.ai`; the agents are self-hosted workers that plug into it.
- **Agent-to-agent discovery** — every worker publishes an A2A card, and the
  router can quote a specialist without a human reading three listings.
- **NEAR ecosystem fit** — no proprietary chain code is required. The agents are
  ordinary HTTP services that can be paid per job in the market, and the same
  pattern extends to NEAR Intents or Chain Signatures for cross-chain payouts.

Primary track: **Agent Economy (`agents`)**. Secondary: **Autonomous Business
(`business`)** — three priced services, one operator, published SLAs and a
repeatable delivery process.

## Architecture

```text
Buyer (human or agent)
  │  "five vertical clips with captions from this podcast"
  ▼
Fleet router  ── fleet.py serve ──  /v1/route, A2A card, dashboard
  │  quote: agent + price + SLA + hire link
  ▼
NEAR AI Agent Market  ── listing, hire, files, buyer messages
  │  signed hire.created  (HMAC, 300 s window, dedupe)
  │  or 30 s polling fallback
  ▼
Self-hosted worker
  ├── brand-checker    : RDAP/WHOIS, X/GitHub/YouTube/TikTok, USPTO/TMview
  ├── media-generator  : Pollinations / LTX-Video / Edge TTS + ffmpeg validation
  └── video-clipper    : ffmpeg analysis + Whisper boundaries + ASS captions
  │  start → progress → upload files → submit → revisions
  ▼
Buyer receives a Markdown report + files
```

The verifier is intentionally separate from the workers: it reads only public
surfaces (listing HTML, health JSON, agent card, test artifact), so a green
check means a buyer can reach the agent, not just that a process is running.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the protocol details and failure
handling.

## Quickstart

Requirements: Python 3.11+ (standard library only), `git`. The individual
services need their own runtimes (ffmpeg, Whisper, `yt-dlp`, `edge-tts`); see
each service README.

```bash
# 1. Fleet control plane (no dependencies)
python3 fleet.py status
python3 fleet.py route "generate three square product photos of a ceramic mug"
python3 fleet.py brand vellumforge
python3 fleet.py serve --port 8780      # dashboard at http://127.0.0.1:8780

# 2. Tests
python3 -m unittest discover -s tests -v

# 3. Paced demo (about 75 seconds of live checks)
./demo.sh
```

Or use the Makefile:

```bash
make test
make status
make route BRIEF="five vertical clips with captions"
make demo
make serve
```

## Tests

```text
$ python3 -m unittest discover -s tests -v
Ran 17 tests in 0.008s
OK
```

The suite covers listing JSON-LD parsing, delivered-job parsing, the router
(including empty and ambiguous briefs), catalog invariants, live/degraded/down
status aggregation with mocked HTTP, Markdown output, and a repository scan for
credential shapes. No network is required.

The workers' own test evidence:

- Media generator: [`services/media-generator/TEST_REPORT.md`](services/media-generator/TEST_REPORT.md)
  and `tests/test_market_mock.py`.
- Video clipper: public job `e720fdfa-c584-4929-87a1-f624ce97a0e7`, report
  linked from its README.
- Brand checker: stable report <https://x0.at/nmZT.md> and JSON
  <https://x0.at/XLIs.json>.

## Router API

`python3 fleet.py serve` exposes:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Live dashboard |
| `GET` | `/healthz` | Fleet health |
| `GET` | `/.well-known/agent-card.json` | A2A card for the router |
| `GET` | `/v1/agents` | Catalog with prices and skills |
| `POST` | `/v1/route` | `{"brief":"..."}` → recommendation, quote, alternatives |
| `POST` | `/api/brand-check` | `{"name":"vellumforge"}` → real public report (rate-limited) |

The router is deterministic and explainable: keyword weights, a confidence
score, and the matched terms are returned. Ambiguous or unknown briefs return
`needs_clarification` instead of a guess.

## Repository layout

```text
fleet.py                  verifier, router, dashboard, HTTP API
fleet.json                the three-agent catalog
demo.sh                   paced live demo (terminal)
tests/                    17 offline unit tests + fixtures
evidence/                 real status runs, screenshots, demo video, brand report
services/brand-checker/   full source + README
services/media-generator/ full source + README + test report
services/video-clipper/   full source + README
docs/DEMO.md              90-second storyboard and exact commands
docs/X_POST.md            draft X post and media notes (do not publish)
docs/SUBMISSION.md        hackathon form fields, payload and blockers
ARCHITECTURE.md           protocol, state and failure handling
```

## Honest limitations

- **No paid deliveries yet.** All three listings show `Delivered jobs: 0` at
  2026-11-09. Public test hires and real public reports exist; revenue does not.
  The submission does not claim otherwise.
- **Quick tunnels are not permanent hosting.** The dashboard and agent health
  URLs are Cloudflare quick tunnels. If a tunnel rotates, the market listing
  stays up but the health check fails until the service re-asserts its webhook.
- **The media generator's AI video route is quota-limited.** Hugging Face
  ZeroGPU allows about 2 GPU-minutes per IP per 24 hours; the agent labels a
  CC BY 4.0 stock fallback instead of pretending it generated the clip.
- **Trademark checks are not legal advice**, and Instagram handle availability
  cannot be confirmed from public signals.
- **The router is keyword-based, not an LLM.** It is deterministic and cheap;
  it will not understand unusual phrasing outside its catalog.
- **The fleet dashboard does not process payments.** Hiring happens on the
  market listing; the router only quotes and links.

## Hackathon notes

- Built 2026-11-08 to 2026-11-09, inside the NEAR HACKATHON window
  (2026-10-19 announcement to 2026-11-14T23:59Z). No pre-hackathon code
  baseline is claimed.
- One submission, one repository, three independent live agents.
- License: MIT. The organizers may use the name, screenshots, video and repo
  link for promotion; the team keeps all rights.

## Links

- Market: <https://market.near.ai>
- Brand checker: <https://market.near.ai/a/brandnamechecker>
- Media generator: <https://market.near.ai/a/media-studio-agent>
- Video clipper: <https://market.near.ai/a/clipper-agent>
- Live dashboard: <https://massage-ralph-promptly-everyone.trycloudflare.com>
- Demo video: [`evidence/demo.mp4`](evidence/demo.mp4)
