# Architecture

## Design in one sentence

Three self-hosted HTTP agents sell narrow, verifiable jobs on NEAR AI Agent
Market; a dependency-free control plane verifies their public surfaces and
routes a brief to the right one with a quote.

## Components

| Component | Process | Responsibility |
|---|---|---|
| Fleet control plane | `fleet.py` | Verify listings/health/cards/tests, route briefs, serve dashboard + API |
| Brand checker | `services/brand-checker/app.py` | Registry, social, app-store and trademark research |
| Media generator | `services/media-generator/app.py` | Images, short video, voiceover; provider reporting and content rules |
| Video clipper | `services/video-clipper/app.py` | Analysis, boundary snapping, rendering, captions, revisions |
| Market | `market.near.ai` | Discovery, hire, buyer messages, file storage, payment |

The workers are intentionally independent. Each has its own agent token, webhook
secret, port, state directory and systemd unit. Losing one does not take down
the others, and the verifier reports per-agent status rather than a single
all-or-nothing flag.

## Hire sequence

```text
Market                         Worker
  │  POST /webhook                │
  │  X-Market-Signature           │
  │  HMAC(timestamp.body)         │
  ├──────────────────────────────▶│  verify, dedupe, return 200 fast
  │                               │  enqueue job
  │  POST /v1/assignments/{id}/start
  │◀──────────────────────────────┤
  │  POST /v1/assignments/{id}/progress
  │◀──────────────────────────────┤  throttled to 30 s
  │  POST /v1/files → PUT → complete
  │◀──────────────────────────────┤  every file stored on the market
  │  POST /v1/assignments/{id}/submit
  │◀──────────────────────────────┤  Markdown + file ids
  │  buyer revision                │
  │  POST /v1/jobs/{id}/revise     │
  ├──────────────────────────────▶│  re-render only named clips
```

A 30-second poller (`GET /v1/agents/me/assignments`) is the fallback when a
webhook is lost. The webhook path verifies the timestamp window (300 s) and
deduplicates on `X-Market-Delivery`; the poller reconciles the same assignment
without creating a second job.

## Verification

`fleet.py status` runs four checks per agent, in parallel across agents:

1. **Public listing** — fetch the listing HTML; parse the `schema.org/Service`
   JSON-LD block; check the name and price against `fleet.json`; read the
   public `Delivered jobs` counter.
2. **Health** — `GET /healthz`; accept `ok: true` or `status: "ok"`; require a
   JSON object.
3. **A2A card** — `GET /.well-known/agent-card.json`; require a name, a
   protocol version and at least one skill.
4. **Public test artifact** — fetch a real report or result and require more
   than 100 bytes.

Status aggregation:

- `live` — listing + card + all four checks pass.
- `degraded` — listing and card pass, but health or the test artifact fails.
- `down` — listing or card fails.

This distinction matters: a `down` agent cannot be hired; a `degraded` agent is
still discoverable but may not answer. The check is read-only and uses no
credentials.

## Router

The router is a transparent keyword model, not an LLM:

- Each agent has a list of weighted keywords in `fleet.json`.
- Single-word matches use word boundaries; multi-word phrases score double.
- The top score wins; ties or zero matches return `needs_clarification`.
- The response includes the matched terms, a confidence ratio, the price, the
  SLA and the public hire link.

The router deliberately does not invent capabilities. If a brief does not match
the catalog, it says so.

## State and idempotency

- Job state is written atomically to `state/jobs/{id}.json` (temp file +
  `os.replace`).
- Webhook deliveries are deduplicated by delivery id.
- A revision reuses the original source and re-renders only the clips named by
  the buyer; untouched files keep their previous uploaded ids.
- Source files are deleted within 24 hours by default, or immediately when the
  brief asks for no retention.

## Security boundaries

- Agent tokens are `aat_` tokens scoped to one agent; they live in
  `secrets.json` (mode 600) and never in this repository.
- The owner session is used only by `provision.py` to create/patch the listing
  and set the webhook; job execution uses the agent token.
- Webhook signatures are HMAC-SHA256 over `<timestamp>.<raw body>`, with a
  300-second replay window and a delivery dedupe table.
- The fleet server binds `127.0.0.1` by default, validates brand-check names
  with a strict regex, rate-limits them to one every five seconds, and never
  accepts a URL from the caller.
- No private keys, seed phrases or wallet material are in the repository.

## Failure handling

| Failure | Behaviour |
|---|---|
| Lost webhook | 30 s poller picks up the assignment |
| Duplicate webhook | Delivery id dedupe; one job |
| Provider timeout | Bounded retry, then a clear report/decline |
| Media quota exhausted | Labelled CC BY 4.0 stock fallback, never passed off as generated |
| Source too long | Refused with a clear message (3-hour cap) |
| Unsupported/DRM source | Refused; buyer can attach the file |
| Tunnel rotates | `run.sh` re-asserts the webhook; verifier shows `degraded` until then |
| Market API error | Retry with backoff; no duplicate submit |

## Why no custom NEAR contract

The product is agent commerce, not a new chain primitive. Agent Market already
provides the listing, hire, file and buyer relationship; the agents are ordinary
HTTP workers that plug into it. This keeps the surface small and auditable. A
future version can add NEAR Intents or Chain Signatures for payouts without
changing the worker protocol.

## What is deliberately not built

- A payment escrow contract.
- A general-purpose LLM planner.
- A permanent hosted dashboard (the quick tunnel is a demo convenience).
- Multi-tenant accounts. Each agent is a single-operator service.
