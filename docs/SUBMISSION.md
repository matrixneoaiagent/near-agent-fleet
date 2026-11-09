# NEAR HACKATHON submission — fields, payload, blockers

Status: **prepared, not submitted.** The final form requires a human-confirmed
NEAR payout account and a published X demo post. Neither is invented here.

## Official rules and exact quotes

- Event site: <https://nearhackathon.com>
- Rules for agents/builders: <https://nearhackathon.com/HACKATHON.md>
- Submission form: <https://nearhackathon.com/#apply>
- Submission API: `POST https://nearhackathon.com/api/applications`
- Change request: <https://nearhackathon.com/request-changes>
- Announcement post: <https://x.com/NEARProtocol/status/2112257967818997891>

Deadlines from `HACKATHON.md`:

> Hackathon starts | 2026-11-01T00:00:00Z
> Development ends (submission deadline) | 2026-11-14T23:59:00Z
> Judging starts | 2026-11-16T00:00:00Z
> Results announced by | 2026-11-18T23:59:00Z

Eligibility of projects built on 2026-11-07/08 — **eligible**:

> **Q-001. Code before 2026-11-01?** Yes, from the announcement on
> 2026-10-19 (https://x.com/nearprotocol/status/2112257967818997891). The
> window runs from then to 2026-11-14. Earlier commits are baseline.

> **Q-002. Existing product on another chain?** Allowed. Only the NEAR
> integration built in the window is judged. Disclose in README.

> **Q-003. New repo required?** No. Dedicated branch, or tags pre-hackathon and
> hackathon-submission, plus a README section linking the diff.

The demo post is **mandatory**, and both links are required:

> Post your demo on X: say you built it at NEAR HACKATHON, tag
> [@NEARProtocol](https://x.com/NEARProtocol) and
> [#NEARHackathon](https://x.com/hashtag/NEARHackathon), and attach the demo
> video or screenshots.

> Submit the repository link and the link to your demo post. Both are required.

Judging (from the site FAQ):

> Claude Code & Codex screen every project first: they check that it runs and
> does what the README says. The human judges above make the decisions.
> Previous NEAR hackathons evaluated technical implementation, design, impact,
> originality and sustainability — a good guide to prepare by.

Other constraints that matter:

- No sign-up; the final submission is the only registration.
- No wallet connection is needed for the form.
- One submission per project; one repository can be submitted only once.
- One person may submit at most 2 different projects (Q-015). This is one.
- Repo must be public by 2026-11-14T23:59Z and stay public through judging.
- License: any; MIT is used here (Q-014).
- Local-only demo is allowed if the repo is public and reproducible; a hosted
  demo is a plus (Q-012). This submission has both.

## Track choice

As of 2026-11-09, the public project directory reported:

| Track | Projects | Prize places |
|---|---:|---:|
| `agents` — Agent Economy | 176 | 3 |
| `business` — Autonomous Business | 64 | 3 |
| `security` — AI Security & Defense | 179 | 3 |
| `swarms` — AI Swarms | 31 | 3 |
| `open` — Open Innovation | 65 | 3 |

Source: `https://nearhackathon.com/api/projects?page=1&track=agents` (and the
same query per track). Counts change daily.

Ranking for this project:

1. **`agents` (primary, best fit).** The product is literally agents that earn
   by being hired, with prices, SLAs and a public hire link. Most crowded, but
   the track description matches the work without stretching.
2. **`business` (secondary, strong).** Three priced services, one operator,
   published SLAs, a repeatable delivery process and a control plane. Less
   crowded, but the project is not a full treasury/procurement business.
3. **`open` (fallback).** Fits as an agent-commerce control plane, but weaker
   than the two above.
4. **`security` (not recommended).** No spending policy, TEE or key-isolation
   primitive in this submission.

Recommendation: submit `tracks: ["agents", "business"]` — both are honest fits,
and the rules allow every track a project fits.

## Proposed form values

| Field | Value | Status |
|---|---|---|
| `projectName` | `NEAR Agent Fleet` | Ready |
| `leadName` | `Matrix Neo` | Proposed from the GitHub identity; human must confirm |
| `email` | `<account email from the local account record>` | Human must confirm; not committed |
| `size` | `1` | Ready |
| `tracks` | `["agents", "business"]` | Ready |
| `nearAddress` | **MISSING** | **Hard blocker — human must provide a mainnet NEAR account they control** |
| `projectUrl` | `https://github.com/matrixneoaiagent/near-agent-fleet` | Public repo created and pushed |
| `demoUrl` | **MISSING** | **Blocker — parent posts the X demo, then supplies the URL** |
| `idea` | see below | Ready |
| `consent` | `true` | Only after the human reads the contact notice verbatim |

No `website` field: the API treats a non-empty `website` as a spam trap and
rejects the submission.

## Draft `idea` (about 900 characters)

> Three paid agents, one operator, live on NEAR AI Agent Market. A buyer
> describes a job in plain language; the fleet verifies every listing, health
> endpoint and A2A card, routes the brief to the right specialist, and returns a
> price, SLA and public hire link. The agents are self-hosted HTTP workers that
> implement the official market protocol: signed webhooks, polling fallback,
> start/progress, file upload, delivery and revisions. The brand checker returns
> domains, handles, app stores and trademarks with evidence per row; the media
> generator returns images, video or voiceover; the clipper returns
> platform-ready clips with captions and a reason per cut. The demo runs a real
> brand check, not a mock. NEAR Agent Market is the discovery and commercial
> rail; each agent is an independent business. Source, tests and honest limits:
> https://github.com/matrixneoaiagent/near-agent-fleet

## Draft X post

Use [`docs/X_POST.md`](X_POST.md) for the full thread. Main post (276 characters):

```text
AI agents shouldn't need hand-wired hiring.

I built NEAR Agent Fleet: 3 hireable agents on @NEARProtocol's Agent Market — brand check, media, video clipping — one control plane that verifies them and routes a brief.

Built at NEAR HACKATHON. Demo + 17 tests. #NEARHackathon 🧵
```

Attach `evidence/demo.mp4` (89 s, 1280×720). Replies add the real brand-check
result, the repo/listings, and NEAR's role. Do not claim revenue, users or paid
deliveries.

## Exact submission steps

1. Human: confirm `leadName`, `email`, and that `matrixneoaiagent` is the
   intended identity.
2. Human: provide a mainnet NEAR account for the prize (`team.near` or a
   64-character implicit account) that they control. Do not create or invent one.
3. Public repo: <https://github.com/matrixneoaiagent/near-agent-fleet>. Verify
   `git status` and that no `secrets.json` is tracked before submitting.
4. Human: publish the X post with the video attached; copy the status URL.
5. Generate `requestId` once: `uuidgen | tr 'A-Z' 'a-z'` (reuse on retries).
6. Print the full JSON payload (below) and have the human read it.
7. POST once:

```bash
curl -sS -X POST https://nearhackathon.com/api/applications \
  -H 'Content-Type: application/json' \
  -d @payload.json
```

8. `201` means saved. `400` means fix `fields` and resend with the same
   `requestId`. `403` means submissions are closed or cross-site. `409` means
   the repo/email was already used. `503` means retry the same `requestId`.
9. If a field is wrong after saving, use
   <https://nearhackathon.com/request-changes>; do not submit again.

## Payload template (do not send until filled and confirmed)

```json
{
  "requestId": "<UUID, generated once>",
  "projectName": "NEAR Agent Fleet",
  "leadName": "<human-confirmed name>",
  "email": "<human-confirmed email>",
  "size": "1",
  "tracks": ["agents", "business"],
  "nearAddress": "<human-controlled mainnet NEAR account>",
  "projectUrl": "https://github.com/matrixneoaiagent/near-agent-fleet",
  "demoUrl": "<published X status URL>",
  "idea": "<the 900-character text above>",
  "consent": true
}
```

## Blockers

1. **NEAR payout address is missing.** The knowledge base has no NEAR account
   and the Agent Market owner record has no `near_address`. A prize cannot be
   paid without a real, human-controlled mainnet account. This is a hard stop.
2. **Demo post is not published.** The parent session posts it; the URL is
   required by the form.
3. **Human confirmation** of the lead name, email and consent notice.
4. **Public repo** is created and public. Confirm the commit you want judged
   and that no `secrets.json` is tracked.

No wallet signature or login is required by the form itself. The submission
API is unauthenticated; the blockers are missing human inputs, not a technical
login wall.
