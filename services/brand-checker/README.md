# Brand Name Availability Checker

Self-hosted HTTP agent for **NEAR AI Agent Market**, Research category.

- **Listing:** <https://market.near.ai/a/brandnamechecker>
- **Agent ID:** `dcc82913-07ad-4774-8c97-c78a2a2edd42`
- **Handle:** `brandnamechecker`
- **Price:** 5.00 USD per check (1–5 names)
- **Delivery SLA:** 1 hour
- **Runtime:** `http` — signed webhook + 30-second polling fallback

One candidate name in. One decision-ready report out: registry domains, public
social handles, Apple/Google app stores and trademark databases, with a UTC
check time and evidence URL on every row.

## What is checked

- **Domains** on the requested extensions via registry RDAP, confirmed with
  registry WHOIS where published.
- **Social handles** on X, GitHub, YouTube, TikTok and Instagram. Each result is
  `available`, `taken`, or `couldn't confirm` with the reason. Instagram does not
  expose a public availability signal, so it is never guessed.
- **App stores:** exact-name apps in the Apple App Store and Google Play.
- **Trademarks:** live US federal marks from USPTO Trademark Search and other
  countries through TMview. Each possible conflict includes the registration or
  application number, status, owner and a text-similarity score.

Every report carries the disclaimer that trademark similarity is not legal
advice.

## Running it

```bash
cd services/brand-checker
python3 -m venv .venv && . .venv/bin/activate
./start.sh                 # app + cloudflared quick tunnel, updates the webhook
./run.sh                   # foreground supervisor for systemd
python3 provision.py --status
```

The service uses the Python standard library only. `secrets.json` (mode 600)
holds the `aat_` agent token, webhook secret and test key. It is not in this
repository.

### Endpoints

| Purpose | Endpoint |
|---|---|
| Health | `GET /healthz` |
| A2A agent card | `GET /.well-known/agent-card.json` |
| A2A JSON-RPC | `POST /a2a/v1` |
| Signed marketplace webhook | `POST /webhook` |
| Direct test hire | `POST /v1/hire` |
| JSON check | `POST /v1/check` |
| Job status | `GET /v1/jobs/{id}` |
| Human report | `GET /r/{id}` |
| Machine result | `GET /v1/jobs/{id}/result.json` |

A direct check request:

```bash
curl -X POST "$BASE/v1/check" \
  -H 'content-type: application/json' \
  -d '{"names":["Vellumforge"],"domains":[".studio",".dev"],"platforms":["x"],"countries":[]}'
```

## Protocol

The marketplace posts HMAC-SHA256 signed `hire.created` events to `/webhook`
(over `<timestamp>.<raw body>`, `X-Market-Signature`, 300-second window,
deduplicated on `X-Market-Delivery`). The worker returns 200 before working,
then calls `start`, posts progress, writes a JSON result, uploads it through
`POST /v1/files`, and submits the Markdown report. A 30-second poller is the
fallback when a webhook is lost.

## Test evidence

- Stable public report: <https://x0.at/nmZT.md>
- JSON result: <https://x0.at/XLIs.json>
- Live demo check (`.studio`, `.dev`, X): real public job, 2026-11-09, linked
  from the fleet demo video.

## Honest limitations

- Instagram, Facebook, Reddit and Threads do not expose a reliable public
  handle-availability signal; requested handles there are reported as
  `couldn't confirm`, not guessed.
- A domain is "available" only when the registry has no record at check time.
  Premium/reserved status and final price are set by the registrar.
- Trademark text similarity is not a legal similarity judgment.
- Public platform APIs can rate-limit (GitHub occasionally returns HTTP 403);
  the report says so instead of inventing a result.
- The public URL is a Cloudflare quick tunnel, stable while the process runs.
  For permanent hosting, point a named tunnel or a small VPS at port 8791 and
  update the listing webhook.
