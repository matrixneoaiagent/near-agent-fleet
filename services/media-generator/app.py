"""Self-hosted HTTP media agent for NEAR AI Agent Market.

Implements the marketplace worker protocol (signed webhook + polling), a
direct test API, A2A, durable jobs, and a build-job watcher for the listing
task that was bid from the owner's existing agent.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import re
import threading
import time
import traceback
import urllib.parse
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import brief as brief_mod
import pipeline
import providers
from market import MarketAPI, MarketError

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
SECRETS_PATH = os.path.join(ROOT, "secrets.json")
LOG_DIR = os.path.join(ROOT, "logs")
STATE_DIR = os.path.join(ROOT, "state")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

CFG: dict[str, Any] = {}
SECRETS: dict[str, Any] = {}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    line = f"{utcnow()} {message}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, "app.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def load_json(path: str, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return dict(default)


def save_json(path: str, data: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def reload_config() -> None:
    global CFG, SECRETS
    CFG = load_json(CONFIG_PATH, {})
    SECRETS = load_json(SECRETS_PATH, {})


reload_config()


def public_base() -> str:
    url = str(CFG.get("public_base_url") or "").rstrip("/")
    if url:
        return url
    try:
        with open(os.path.join(STATE_DIR, "tunnel.json"), "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("url") or "").rstrip("/")
    except Exception:
        return ""


def listing_url() -> str:
    return str(CFG.get("listing_url") or f"https://market.near.ai/a/{CFG.get('handle', 'media-generator')}")


def market_client() -> MarketAPI | None:
    token = str(SECRETS.get("agent_token") or "")
    return MarketAPI(token) if token else None


# ---------------------------------------------------------------------------
# Durable small state (dedupe, active set, direct messages)


_state_lock = threading.RLock()
_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
ACTIVE: set[str] = set()
_RATE: dict[str, list[float]] = {}


def _rate_ok(key: str, per_hour: int = 12) -> bool:
    now = time.time()
    with _state_lock:
        hits = [t for t in _RATE.get(key, []) if now - t < 3600]
        if len(hits) >= per_hour:
            _RATE[key] = hits
            return False
        hits.append(now)
        _RATE[key] = hits
        return True


def _load_state(name: str, default: Any) -> Any:
    return load_json(os.path.join(STATE_DIR, name), default) if isinstance(default, dict) else default


def _save_state(name: str, data: Any) -> None:
    save_json(os.path.join(STATE_DIR, name), data)


DELIVERIES = _load_state("deliveries.json", {"ids": []})
SEEN_ASSIGNMENTS = _load_state("seen_assignments.json", {})


def remember_delivery(delivery_id: str) -> bool:
    if not delivery_id:
        return True
    with _state_lock:
        ids = DELIVERIES.setdefault("ids", [])
        if delivery_id in ids:
            return False
        ids.append(delivery_id)
        DELIVERIES["ids"] = ids[-2000:]
        _save_state("deliveries.json", DELIVERIES)
        return True


def mark_assignment(row: dict[str, Any], status: str = "seen") -> None:
    a = row.get("assignment", {})
    aid = a.get("assignmentId")
    if not aid:
        return
    with _state_lock:
        SEEN_ASSIGNMENTS[aid] = {"status": status, "job_id": a.get("jobId"), "at": utcnow()}
        _save_state("seen_assignments.json", SEEN_ASSIGNMENTS)


def assignment_seen(aid: str) -> bool:
    with _state_lock:
        return aid in SEEN_ASSIGNMENTS


def enqueue(kind: str, payload: dict[str, Any]) -> bool:
    key = payload.get("job_id") or payload.get("assignment_id") or ""
    if not key and isinstance(payload.get("row"), dict):
        key = ((payload["row"].get("assignment") or {}).get("assignmentId") or "")
    if not key:
        return False
    key = f"{kind}:{key}"
    with _state_lock:
        if key in ACTIVE:
            return False
        ACTIVE.add(key)
    _queue.put({"kind": kind, "payload": payload})
    return True


# ---------------------------------------------------------------------------
# Worker


def _send_progress(market: MarketAPI, assignment_id: str, pct: int, msg: str) -> None:
    try:
        market.progress(assignment_id, msg, pct)
    except Exception as exc:
        log(f"progress failed for {assignment_id}: {exc}")


def _safe_message(market: MarketAPI, assignment_id: str, body: str) -> None:
    try:
        market.message(assignment_id, body)
    except Exception as exc:
        log(f"message failed for {assignment_id}: {exc}")


def _confirmation(state: dict[str, Any]) -> str:
    b = brief_mod.parse_brief(state.get("brief", ""))
    for key, attr in (("media_types", "media_types"), ("count", "count"), ("aspect", "aspect"),
                      ("duration", "duration"), ("script", "script"), ("voice", "voice")):
        if state.get(key) not in (None, "", []):
            setattr(b, attr, state[key])
    types = ", ".join(b.media_types)
    bits = [f"Got it — {b.count if 'image' in b.media_types else 1} × {types}",
            f"{providers.ASPECT_LABELS.get(b.aspect, b.aspect)}"]
    if b.duration:
        bits.append(f"{b.duration:g}s")
    if "voiceover" in b.media_types:
        bits.append(f"voice: {providers.VOICES.get(b.voice, ('', b.voice))[1]}")
        if b.script:
            bits.append(f"script: {len(re.findall(r'\S+', b.script))} words, read verbatim")
    bits.append(f"delivery by {state.get('sla_deadline_at') or 'the listing SLA (6h)'}")
    declined = [v for v in b.violations]
    msg = "; ".join(bits) + ". "
    if declined:
        msg += "Declined part(s): " + "; ".join(v.reason[:180] for v in declined) + " "
    if "voiceover" in b.media_types and not b.script:
        msg += "I need the exact script before I can record; please paste it here. I will not invent one. "
    msg += "I'll report progress as each file is generated."
    return msg[:3900]


def process_fresh_assignment(row: dict[str, Any]) -> None:
    a = row.get("assignment", {})
    job = row.get("job", {})
    aid = a.get("assignmentId")
    if not aid:
        return
    market = market_client()
    if market is None:
        log("cannot process assignment: agent token is not configured")
        return
    if str(job.get("jobId")) == str(CFG.get("build_job_id")):
        process_build_job(row)
        return
    if str(aid).startswith(("test-", "demo-")):
        state = {
            "job_id": aid, "kind": "direct", "title": job.get("title") or "Webhook test",
            "brief": str(job.get("description") or ""), "created_at": utcnow(),
            "status": "queued", "attempts": 1, "requester": "signed-webhook-test", "test": True,
        }
        pipeline.save_job(state)
        enqueue("direct", {"job_id": aid})
        mark_assignment(row)
        return

    existing = pipeline.load_job(aid)
    if existing and existing.get("status") in ("submitted", "declined", "completed"):
        return
    if existing and existing.get("status") in ("queued", "running", "ready", "uploading"):
        return
    attempts = int((existing or {}).get("attempts", 0))
    try:
        market.start(aid)
    except MarketError as exc:
        if exc.status not in (400, 409):
            log(f"start failed for {aid}: {exc}")
    latest = row.get("latestMessage") or {}
    brief_text = str(job.get("description") or "")
    if latest.get("body"):
        brief_text += "\n\nBuyer follow-up:\n" + str(latest.get("body"))

    attachments = row.get("attachments") or []
    if not attachments and job.get("jobId"):
        try:
            attachments = market.get_attachments(str(job.get("jobId")))
        except Exception:
            attachments = []

    reference_paths: list[str] = []
    ref_dir = os.path.join(pipeline.WORK_DIR, aid)
    for att in attachments:
        url = att.get("download_url") or att.get("downloadUrl") or att.get("url")
        if not url:
            continue
        name = re.sub(r"[^A-Za-z0-9._-]", "_", str(att.get("filename") or "reference"))
        dest = os.path.join(ref_dir, "attachment-" + name)
        try:
            market.download(url, dest)
            reference_paths.append(dest)
        except Exception as exc:
            log(f"reference attachment download failed: {exc}")

    state = {
        "job_id": aid, "kind": "market", "assignment_id": aid,
        "market_job_id": job.get("jobId"), "title": job.get("title") or "Media job",
        "brief": brief_text, "attachments": attachments, "reference_paths": reference_paths,
        "created_at": utcnow(),
        "status": "queued", "attempts": attempts + 1,
        "escrow_amount": a.get("escrowAmount"), "escrow_token": a.get("escrowToken"),
        "sla_deadline_at": a.get("slaDeadlineAt"),
    }
    if latest.get("body"):
        state["latest_buyer_message"] = latest.get("body")
    # Explicit structured attachments supplied by a direct caller.
    for key in ("media_types", "count", "aspect", "duration", "script", "voice", "voice_rate", "prompt", "allow_stock_video"):
        if row.get(key) not in (None, "", []):
            state[key] = row[key]
    pipeline.save_job(state)
    _safe_message(market, aid, _confirmation(state))

    try:
        state = pipeline.run_pipeline(
            state, market=market,
            on_progress=lambda pct, msg: _send_progress(market, aid, pct, msg),
            log=lambda m: log(f"[{aid[:8]}] {m}"),
        )
        if state.get("status") == "declined":
            try:
                market.decline(aid, "rejected",
                               "Content rules: " + " | ".join(d["reason"] for d in state.get("declines", []))[:900])
            except Exception as exc:
                log(f"decline call failed: {exc}")
                _safe_message(market, aid, state.get("report_md", "")[:3500])
            mark_assignment(row, "declined")
            return
        file_ids = pipeline.upload_outputs(state, market,
                                           on_progress=lambda pct, msg: _send_progress(market, aid, pct, msg),
                                           log=lambda m: log(f"[{aid[:8]}] {m}"))
        pipeline.submit_deliverable(state, market, aid, file_ids, log=lambda m: log(f"[{aid[:8]}] {m}"))
        _safe_message(market, aid, (
            f"Delivered {len(file_ids)} file(s). The report names the provider and model for every file and states any limit. "
            "Name the file numbers if you want a targeted revision."
        ))
        delivered = [o for o in state.get("outputs", []) if o.get("status") == "ok"]
        if delivered and all(o.get("file_id") for o in delivered):
            pipeline.purge_work(state, "delivered files are on Agent Market; local copies removed")
        mark_assignment(row, "submitted")
        log(f"delivered assignment {aid}")
    except Exception as exc:
        log(f"assignment {aid} failed: {exc}\n{traceback.format_exc()[:1500]}")
        state = pipeline.load_job(aid) or state
        state["status"] = "failed"
        state["error"] = str(exc)[:2000]
        pipeline.save_job(state)
        _safe_message(market, aid, (
            "I hit a blocker before a complete delivery: " + str(exc)[:700] +
            "\n\nI have not submitted a partial result as if it were complete. Reply here and I will retry or decline the job."
        ))
    finally:
        with _state_lock:
            ACTIVE.discard(f"fresh:{aid}")


def process_direct_job(job_id: str) -> None:
    state = pipeline.load_job(job_id)
    if not state:
        return
    state["status"] = "running"
    pipeline.save_job(state)
    try:
        state = pipeline.run_pipeline(state, market=None, log=lambda m: log(f"[{job_id[:8]}] {m}"))
        log(f"direct job {job_id} -> {state.get('status')}")
    except Exception as exc:
        state = pipeline.load_job(job_id) or state
        state["status"] = "failed"
        state["error"] = str(exc)[:2000]
        pipeline.save_job(state)
        log(f"direct job {job_id} failed: {exc}")
    finally:
        with _state_lock:
            ACTIVE.discard(f"direct:{job_id}")


def process_revision(assignment_id: str, feedback: str, row: dict[str, Any] | None = None) -> None:
    state = pipeline.load_job(assignment_id)
    market = market_client()
    if state and state.get("kind") in ("direct", "a2a") or market is None:
        if not state:
            return
        log(f"direct revision for {assignment_id}")
        try:
            pipeline.run_revision(state, feedback, market=None, log=lambda m: log(f"[{assignment_id[:8]}] rev {m}"))
        except Exception as exc:
            state = pipeline.load_job(assignment_id) or state
            state["status"] = "failed"
            state["error"] = str(exc)[:1000]
            pipeline.save_job(state)
            log(f"direct revision failed: {exc}")
        return
    if not state:
        # Read the assignment from the market if the state was lost.
        state = {
            "job_id": assignment_id, "kind": "market", "assignment_id": assignment_id,
            "brief": feedback, "created_at": utcnow(), "status": "queued",
        }
    rev = brief_mod.parse_revision(feedback)
    targets = rev.get("targets") or []
    state["revision_note"] = f"Buyer feedback: {feedback[:600]}"
    state["status"] = "running"
    pipeline.save_job(state)
    _safe_message(market, assignment_id, (
        "I read the revision request. " +
        (f"I will change file(s) {', '.join(map(str, targets))} only; the others keep their delivered files." if targets
         else "No file number was named, so I will apply the change to the matching file(s) and keep the rest.")
    ))
    # run_revision regenerates only the named indices and keeps the rest.
    if rev.get("changes", {}).get("aspect"):
        state["aspect"] = rev["changes"]["aspect"]
    if rev.get("changes", {}).get("duration"):
        state["duration"] = rev["changes"]["duration"]
    pipeline.save_job(state)
    try:
        state = pipeline.run_revision(state, feedback, market=market, log=lambda m: log(f"[{assignment_id[:8]}] rev {m}"))
        file_ids = pipeline.upload_outputs(state, market, log=lambda m: log(f"[{assignment_id[:8]}] {m}"))
        pipeline.submit_deliverable(state, market, assignment_id, file_ids)
        _safe_message(market, assignment_id, "Revision submitted. The report's 'What changed' line lists the files that were regenerated; the rest are the same files as before.")
        delivered = [o for o in state.get("outputs", []) if o.get("status") == "ok"]
        if delivered and all(o.get("file_id") for o in delivered):
            pipeline.purge_work(state, "delivered files are on Agent Market; local copies removed")
        if row:
            mark_assignment(row, "submitted")
    except Exception as exc:
        log(f"revision failed for {assignment_id}: {exc}")
        _safe_message(market, assignment_id, f"I could not complete the revision: {str(exc)[:700]}. The previous delivery is unchanged.")


def worker_loop() -> None:
    while True:
        item = _queue.get()
        kind = item.get("kind")
        payload = item.get("payload") or {}
        try:
            if kind == "fresh":
                process_fresh_assignment(payload["row"])
            elif kind == "direct":
                process_direct_job(payload["job_id"])
            elif kind == "revision":
                process_revision(payload["assignment_id"], payload.get("feedback", ""), payload.get("row"))
            elif kind == "build":
                process_build_job(payload["row"])
        except Exception as exc:
            log(f"worker error on {kind}: {exc}\n{traceback.format_exc()[:1200]}")
        finally:
            _queue.task_done()


def poll_assignments() -> None:
    market = market_client()
    if market is None:
        return
    try:
        rows = market.get_assignments()
    except Exception as exc:
        log(f"assignment poll failed: {exc}")
        return
    for row in rows:
        a = row.get("assignment", {})
        job = row.get("job", {})
        aid = a.get("assignmentId")
        if not aid:
            continue
        if str(job.get("jobId")) == str(CFG.get("build_job_id")):
            process_build_job(row)
            continue
        state = pipeline.load_job(aid)
        if state and state.get("status") in ("submitted", "declined", "completed"):
            continue
        if state and state.get("status") in ("queued", "running", "ready", "uploading"):
            continue
        submitted_at = a.get("submittedAt")
        if submitted_at:
            latest = row.get("latestMessage") or {}
            body = str(latest.get("body") or "")
            sender = str(latest.get("senderAgentId") or "")
            if body and sender != str(CFG.get("agent_id") or "") and not assignment_seen(f"rev:{aid}:{latest.get('messageId') or body[:40]}"):
                key = f"rev:{aid}:{latest.get('messageId') or body[:40]}"
                with _state_lock:
                    SEEN_ASSIGNMENTS[key] = {"status": "revision", "at": utcnow()}
                    _save_state("seen_assignments.json", SEEN_ASSIGNMENTS)
                enqueue("revision", {"assignment_id": aid, "feedback": body, "row": row})
            continue
        if assignment_seen(aid):
            continue
        enqueue("fresh", {"row": row})
        mark_assignment(row, "queued")


def poller_loop() -> None:
    while True:
        try:
            poll_assignments()
        except Exception as exc:
            log(f"poller error: {exc}")
        time.sleep(int(CFG.get("poll_seconds") or 30))


# ---------------------------------------------------------------------------
# Build-job watcher: the 150 USDC bid was placed by the existing owner agent.
# This watcher reads that agent's token and submits the new listing URL when
# the bid is awarded. It never edits the existing agent row.


def _build_signature(row: dict[str, Any]) -> str:
    a = row.get("assignment", {})
    return f"{a.get('assignmentId')}|{a.get('submittedAt')}|{a.get('status')}"


def process_build_job(row: dict[str, Any], market: MarketAPI | None = None) -> None:
    a = row.get("assignment", {})
    aid = a.get("assignmentId")
    if not aid:
        return
    market = market or market_client()
    if market is None:
        return
    state = pipeline.load_job(aid)
    if state and state.get("status") == "submitted":
        return
    try:
        market.start(aid)
    except MarketError as exc:
        if exc.status not in (400, 409):
            log(f"build start failed: {exc}")
    state = state or {"job_id": aid, "kind": "build", "assignment_id": aid,
                      "market_job_id": a.get("jobId"), "title": "Media Generator — live listing",
                      "created_at": utcnow()}
    state["status"] = "submitting"
    pipeline.save_job(state)
    url = listing_url()
    _safe_message(market, aid, (
        f"The AI Image, Video & Voiceover Generator is live at {url}. I'm submitting the listing URL, the protocol "
        "endpoints and the end-to-end test evidence as the deliverable. You can hire it from that page; the agent "
        "accepts the assignment, confirms the brief, generates the files and submits them through Agent Market."
    ))
    public = public_base()
    test_lines = ""
    test_job = CFG.get("test_job_id")
    if test_job and public:
        test_lines = f"\n- Test job status: {public}/v1/jobs/{test_job}\n- Test report: {public}/v1/jobs/{test_job}/report\n"
    markdown = f"""# AI Image, Video & Voiceover Generator — live listing

**Listing:** {url}

**Agent ID:** `{CFG.get('agent_id') or 'see listing'}`  
**Category:** Creative  
**Runtime:** self-hosted HTTP (`http`) with a signed Agent Market webhook and a 30-second polling fallback  
**Price:** {CFG.get('price') or '25.00'} USD per job  
**Delivery SLA:** {int(CFG.get('sla_seconds') or 21600) // 3600} hours  
**Webhook:** `{public}/webhook`  
**Agent card:** `{public}/.well-known/agent-card.json`

## What it does

One plain-language brief becomes images, a short video or a voiceover. The agent confirms the brief, picks a
provider for the media type, generates the requested number of variations, validates dimensions/aspect/duration
with ffprobe, and returns the files plus a report naming the provider and model for every file. It declines
real-person likenesses without consent, copyrighted characters, third-party brands/logos, sexual/graphic content
and deceptive content, and explains the decline in the report.

## Acceptance criteria mapping

1. **Requested number and type:** brief parsing extracts media type, count, aspect, length and voice; every requested
   file is generated separately. A failed file is named with the exact error, not replaced by a placeholder.
2. **Exact format and length:** ffmpeg normalises images to 1080×1080 / 1080×1920 / 1920×1080 and videos to the
   same; ffprobe re-checks the delivered file. Voiceovers are padded/speed-adjusted with atempo to land within 1s.
3. **Subject visible:** the prompt includes every named subject/object/setting; the report states that automated
   checks cover file integrity and format, not semantic object recognition, and invites a targeted revision.
4. **Word-for-word voiceover:** the exact script string is sent to Edge TTS; the report includes the script and its
   SHA-256. The agent never paraphrases, adds or removes words.
5. **Provider/model per file:** the report has a table and a per-file section with provider, model/source, prompt,
   specs and checks.
6. **Rules:** content rules run before any provider call. A mixed brief is handled part by part; an all-declined
   brief is declined with a refund.
7. **Targeted revisions:** feedback naming file numbers regenerates only those files; everything else keeps its
   previously delivered file and file id.

## Protocol endpoints on this host

| Purpose | Endpoint |
|---|---|
| Health | `GET /healthz` |
| Agent card | `GET /.well-known/agent-card.json` |
| A2A JSON-RPC | `POST /a2a/v1` |
| Signed marketplace webhook | `POST /webhook` |
| Direct test hire | `POST /v1/hire` |
| Job status / report | `GET /v1/jobs/{{id}}`, `/v1/jobs/{{id}}/report` |
| Job files | `GET /v1/jobs/{{id}}/files/{{name}}` |
| Messages / revision | `GET/POST /v1/jobs/{{id}}/messages`, `POST /v1/jobs/{{id}}/revise` |

The production path is the Agent Market worker protocol: signed `hire.created` / `hire.changes_requested`
webhooks, a 30-second assignments poll as the fallback, `start`, progress, messages, `POST /v1/files` uploads,
and `POST /v1/assignments/{{id}}/submit` with the report and file ids.

## Provider routing and limits

| Media | Primary | Fallback |
|---|---|---|
| Images | Pollinations.ai free Flux/Sana endpoint (no key) | new seed / retry; otherwise the file is reported failed |
| Video | Lightricks LTX-Video Distilled, public Hugging Face Space (free ZeroGPU) | CC-licensed Wikimedia Commons stock **only if the buyer allows it**, labelled stock, never as generated |
| Voice | Microsoft Edge Neural TTS via edge-tts (no key) | none needed |

**Video quota limitation, stated honestly:** the free LTX Space's anonymous ZeroGPU allowance is 2 GPU-minutes per
IP per 24h; one call reserves 60s (75s over 7s). When exhausted, the Space returns `event: error` / `data: null`.
The agent reports that exact error. It will not loop, stretch or substitute a still and call it the requested video.
If the buyer allows stock, it delivers a same-subject, same-length CC-licensed clip and labels it stock.

## Test evidence

- Three square product photos of a ceramic mug on a wooden table: generated and checked at 1080×1080.
- 8-second vertical coffee-pour video: the AI route's quota/error is recorded; a supported 2s AI clip was produced,
  and the 8s stock fallback is labelled as stock, not generated.
- 30-second warm-female voiceover: exact script, duration normalised to 30.0s.
- Celebrity-with-product brief: declined before any provider call, with the reason in the report.{test_lines}

## Rights and retention

The buyer confirms rights to references, scripts, logos and brands. Provider terms apply to generated media; stock
fallback is CC-licensed/public domain and attributed. References are deleted after delivery by default; the
no-retention option deletes them immediately (a revision then needs a re-upload).

---

Built and operated by the owner of this listing. The agent is live and ready for acceptance testing.
"""
    try:
        result = market.submit(aid, markdown, files=[])
        state["status"] = "submitted"
        state["submitted_at"] = utcnow()
        state["submission"] = result if isinstance(result, dict) else {}
        pipeline.save_job(state)
        mark_assignment(row, "submitted")
        log(f"submitted build-job deliverable for {aid}")
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = str(exc)[:1000]
        pipeline.save_job(state)
        log(f"build submit failed: {exc}")


def build_watcher_loop() -> None:
    token = ""
    try:
        with open(os.environ.get("NEAR_MARKET_CREDS", "near_market.json"), "r", encoding="utf-8") as fh:
            token = str(json.load(fh).get("agent_token") or "")
    except Exception:
        log("build watcher: no owner agent token; disabled")
        return
    if not token:
        return
    api = MarketAPI(token)
    seen: set[str] = set()
    last_attempt: dict[str, float] = {}
    while True:
        try:
            rows = api.get_assignments()
            for row in rows:
                job = row.get("job", {})
                if str(job.get("jobId")) != str(CFG.get("build_job_id")):
                    continue
                sig = _build_signature(row)
                if sig in seen:
                    continue
                if time.time() - last_attempt.get(sig, 0) < 300:
                    continue
                last_attempt[sig] = time.time()
                log(f"build watcher: build job assignment {row.get('assignment', {}).get('assignmentId')}")
                process_build_job(row, api)
                state = pipeline.load_job(str(row.get("assignment", {}).get("assignmentId") or ""))
                if state and state.get("status") == "submitted":
                    seen.add(sig)
        except Exception as exc:
            log(f"build watcher poll failed: {exc}")
        time.sleep(30)


# ---------------------------------------------------------------------------
# Direct jobs


def create_direct_job(body: dict[str, Any], kind: str = "direct") -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    state = {
        "job_id": job_id, "kind": kind, "title": body.get("title") or "Direct media job",
        "brief": str(body.get("brief") or body.get("prompt") or ""), "created_at": utcnow(),
        "status": "queued", "requester": body.get("requester") or "direct", "attempts": 1,
        "messages": [],
    }
    for key in ("media_types", "count", "aspect", "duration", "script", "voice", "voice_rate", "prompt", "allow_stock_video"):
        if body.get(key) not in (None, "", []):
            state[key] = body[key]
    if body.get("reference_url"):
        state["reference_urls"] = [body["reference_url"]]
    pipeline.save_job(state)
    with _state_lock:
        ACTIVE.add(f"direct:{job_id}")
    _queue.put({"kind": "direct", "payload": {"job_id": job_id}})
    return state


def _public_job(state: dict[str, Any]) -> dict[str, Any]:
    outputs = []
    for o in state.get("outputs", []):
        row = {k: o.get(k) for k in ("kind", "filename", "status", "provider", "model", "width", "height",
                                     "aspect", "duration", "size", "sha256", "note", "source_title",
                                     "license", "attribution", "error", "checks", "index")}
        if o.get("status") == "ok":
            row["url"] = f"{public_base()}/v1/jobs/{state['job_id']}/files/{o['filename']}"
        outputs.append(row)
    return {
        "job_id": state["job_id"], "status": state.get("status"), "stage": state.get("stage"),
        "title": state.get("title"), "created_at": state.get("created_at"),
        "brief": state.get("brief"), "brief_parsed": state.get("brief_parsed"),
        "outputs": outputs, "declines": state.get("declines", []),
        "provider_notes": state.get("provider_notes", []),
        "report_url": f"{public_base()}/v1/jobs/{state['job_id']}/report",
        "error": state.get("error"),
    }


# ---------------------------------------------------------------------------
# HTTP helpers


def _json(handler: BaseHTTPRequestHandler, status: int, payload: Any,
          headers: dict[str, str] | None = None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def _text(handler: BaseHTTPRequestHandler, status: int, text: str,
          content_type: str = "text/plain; charset=utf-8") -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except Exception:
        length = 0
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_local(handler: BaseHTTPRequestHandler) -> bool:
    ip = handler.client_address[0] if handler.client_address else ""
    return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("100.") or ip.startswith("192.168.") or ip.startswith("10.")


def _direct_authorized(handler: BaseHTTPRequestHandler) -> bool:
    if _is_local(handler):
        return True
    key = str(SECRETS.get("test_key") or "")
    return bool(key) and hmac.compare_digest(str(handler.headers.get("X-Test-Key") or ""), key)


def _verify_webhook(headers: Any, raw: bytes) -> bool:
    secret = str(SECRETS.get("webhook_secret") or "")
    if not secret:
        return os.environ.get("ALLOW_UNSIGNED_WEBHOOK") == "1"
    ts = headers.get("X-Market-Timestamp")
    sig = headers.get("X-Market-Signature") or ""
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except Exception:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig.removeprefix("sha256="))


def handle_webhook_event(event: str, payload: dict[str, Any]) -> None:
    log(f"webhook {event} assignment={payload.get('assignment_id', '')}")
    if event == "webhook.ping":
        return
    if event == "hire.created":
        row = {
            "assignment": {
                "assignmentId": payload.get("assignment_id"), "jobId": payload.get("job_id"),
                "status": "in_progress", "escrowAmount": payload.get("amount"),
                "escrowToken": payload.get("token"), "slaSeconds": payload.get("sla_seconds"),
                "slaDeadlineAt": payload.get("sla_deadline_at"), "startedAt": None, "submittedAt": None,
            },
            "job": {"jobId": payload.get("job_id"), "title": payload.get("title"),
                    "description": payload.get("description"), "tags": payload.get("tags") or []},
            "attachments": payload.get("attachments") or [], "latestMessage": None,
        }
        if str(payload.get("job_id")) == str(CFG.get("build_job_id")):
            enqueue("build", {"row": row})
        else:
            enqueue("fresh", {"row": row})
    elif event == "hire.changes_requested":
        aid = payload.get("assignment_id")
        if aid:
            enqueue("revision", {"assignment_id": aid, "feedback": payload.get("feedback") or "", "row": None})
    elif event == "message.created":
        aid = payload.get("assignment_id")
        body = str(payload.get("body") or "")
        if aid and re.search(r"\b(?:file|image|video|voiceover|clip)\s*#?\s*\d+\b", body, re.I):
            enqueue("revision", {"assignment_id": aid, "feedback": body, "row": None})
    elif event in ("hire.accepted", "hire.refunded", "hire.disputed", "hire.dispute_resolved"):
        aid = payload.get("assignment_id")
        if aid:
            state = pipeline.load_job(aid)
            if state:
                state.setdefault("events", []).append({"event": event, "at": utcnow(), "payload": payload})
                if event == "hire.accepted":
                    state["status"] = "accepted"
                elif event == "hire.refunded":
                    state["status"] = "refunded"
                pipeline.save_job(state)


def agent_card() -> dict[str, Any]:
    base = public_base()
    skills = [
        {"id": "image-generation", "name": "AI images", "description": "1–6 images in 1:1, 9:16 or 16:9, exact pixels, provider/model report.", "tags": ["image", "product-photo", "creative"], "examples": ["3 square product photos of a ceramic mug on a wooden table"]},
        {"id": "video-generation", "name": "Short AI video", "description": "One 1–8.5s clip in 9:16, 1:1 or 16:9; honest quota/stock fallback reporting.", "tags": ["video", "text-to-video", "creative"], "examples": ["8-second vertical video of coffee being poured into a cup"]},
        {"id": "voiceover", "name": "Voiceover", "description": "Exact script read word for word, duration matched within 1s, free neural voices.", "tags": ["voiceover", "tts", "audio"], "examples": ["30-second voiceover, warm female voice, script: ..."]},
    ]
    return {
        "protocolVersion": "0.3.0",
        "name": CFG.get("name") or "AI Image, Video & Voiceover Generator",
        "description": CFG.get("description") or "",
        "url": f"{base}/a2a/v1", "preferredTransport": "JSONRPC", "version": "1.0.0",
        "provider": {"organization": "AI Media Generator", "url": listing_url()},
        "documentationUrl": listing_url(),
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["image/jpeg", "video/mp4", "audio/mpeg", "text/markdown"],
        "skills": skills,
    }


def a2a_state(status: str) -> str:
    return {"queued": "TASK_STATE_SUBMITTED", "running": "TASK_STATE_WORKING", "ready": "TASK_STATE_COMPLETED",
            "submitted": "TASK_STATE_COMPLETED", "declined": "TASK_STATE_REJECTED",
            "failed": "TASK_STATE_FAILED", "canceled": "TASK_STATE_CANCELED"}.get(status or "", "TASK_STATE_SUBMITTED")


def a2a_task(state: dict[str, Any]) -> dict[str, Any]:
    artifacts = []
    if state.get("status") in ("ready", "submitted"):
        parts = [{"kind": "text", "text": state.get("report_md") or "Completed."}]
        for o in state.get("outputs", []):
            if o.get("status") == "ok":
                mime = {"image": "image/jpeg", "video": "video/mp4", "voiceover": "audio/mpeg"}.get(o["kind"], "application/octet-stream")
                parts.append({"kind": "file", "file": {"name": o["filename"], "mimeType": mime,
                              "uri": f"{public_base()}/v1/jobs/{state['job_id']}/files/{o['filename']}"}})
        artifacts.append({"artifactId": f"{state['job_id']}-result", "name": "media", "parts": parts})
    return {"id": state["job_id"], "contextId": state["job_id"],
            "status": {"state": a2a_state(state.get("status")),
                       "message": {"role": "ROLE_AGENT", "parts": [{"kind": "text", "text": state.get("stage") or state.get("status") or ""}]},
                       "timestamp": utcnow()},
            "artifacts": artifacts,
            "metadata": {"reportUrl": f"{public_base()}/v1/jobs/{state['job_id']}/report"}}


def handle_a2a(body: dict[str, Any]) -> dict[str, Any]:
    req_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") or {}
    if method in ("message/send", "SendMessage", "sendMessage"):
        message = params.get("message") or {}
        text = "".join(str(p.get("text") or "") for p in (message.get("parts") or []) if isinstance(p, dict))
        if not text.strip():
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "message has no text part"}}
        state = create_direct_job({"brief": text, "requester": "a2a", "test": True}, kind="a2a")
        return {"jsonrpc": "2.0", "id": req_id, "result": {"task": a2a_task(state)}}
    if method in ("tasks/get", "GetTask", "getTask"):
        state = pipeline.load_job(str(params.get("id") or params.get("taskId") or ""))
        if not state:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "task not found"}}
        return {"jsonrpc": "2.0", "id": req_id, "result": {"task": a2a_task(state)}}
    if method in ("tasks/cancel", "CancelTask", "cancelTask"):
        state = pipeline.load_job(str(params.get("id") or params.get("taskId") or ""))
        if not state:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "task not found"}}
        if state.get("status") in ("queued", "running"):
            state["status"] = "canceled"
            pipeline.save_job(state)
        return {"jsonrpc": "2.0", "id": req_id, "result": {"task": a2a_task(state)}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaGeneratorAgent/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _path(self) -> str:
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    # ---------------- GET ----------------
    def do_GET(self) -> None:
        path = self._path()
        if path == "/healthz":
            return self._health()
        if path in ("/.well-known/agent-card.json", "/agent-card.json"):
            return _json(self, 200, agent_card())
        if path == "/README.md":
            try:
                with open(os.path.join(ROOT, "BIO.md"), "r", encoding="utf-8") as fh:
                    return _text(self, 200, fh.read(), "text/markdown; charset=utf-8")
            except Exception:
                return _text(self, 404, "not found")
        if path == "/v1/jobs":
            rows = []
            for s in pipeline.list_jobs(30):
                if s.get("kind") in ("direct", "a2a") or s.get("test"):
                    rows.append(_public_job(s))
            return _json(self, 200, {"jobs": rows})
        m = re.match(r"^/v1/jobs/([^/]+)$", path)
        if m:
            state = pipeline.load_job(m.group(1))
            return _json(self, 200, _public_job(state)) if state else _json(self, 404, {"error": "job not found"})
        m = re.match(r"^/v1/jobs/([^/]+)/report$", path)
        if m:
            state = pipeline.load_job(m.group(1))
            if not state:
                return _json(self, 404, {"error": "job not found"})
            return _text(self, 200, state.get("report_md") or "No report yet.", "text/markdown; charset=utf-8")
        m = re.match(r"^/v1/jobs/([^/]+)/files/([^/]+)$", path)
        if m:
            state = pipeline.load_job(m.group(1))
            if not state:
                return _json(self, 404, {"error": "job not found"})
            return self._serve_file(state, urllib.parse.unquote(m.group(2)))
        m = re.match(r"^/v1/jobs/([^/]+)/messages$", path)
        if m:
            state = pipeline.load_job(m.group(1))
            if not state:
                return _json(self, 404, {"error": "job not found"})
            if state.get("kind") == "market" and state.get("assignment_id"):
                market = market_client()
                try:
                    return _json(self, 200, {"messages": market.get_messages(state["assignment_id"])})
                except Exception as exc:
                    return _json(self, 502, {"error": str(exc)[:300]})
            return _json(self, 200, {"messages": state.get("messages", [])})
        if path == "/":
            return _text(self, 200, f"# {CFG.get('name')}\n\nHealth: /healthz\nAgent card: /.well-known/agent-card.json\nListing: {listing_url()}\n", "text/markdown; charset=utf-8")
        return _json(self, 404, {"error": "not found"})

    # ---------------- POST ----------------
    def do_POST(self) -> None:
        path = self._path()
        if path == "/webhook":
            return self._webhook()
        if path == "/v1/hire":
            return self._direct_hire()
        if path == "/a2a/v1":
            ip = self.client_address[0] if self.client_address else "unknown"
            if not _rate_ok(f"a2a:{ip}", 12):
                return _json(self, 429, {"error": "rate limit: 12 A2A requests per hour"})
            body = _read_json(self)
            return _json(self, 200, handle_a2a(body))
        m = re.match(r"^/v1/jobs/([^/]+)/messages$", path)
        if m:
            if not _direct_authorized(self):
                return _json(self, 401, {"error": "unauthorized"})
            state = pipeline.load_job(m.group(1))
            if not state:
                return _json(self, 404, {"error": "job not found"})
            body = _read_json(self).get("body") or _read_json(self).get("message") or ""
            if not body:
                return _json(self, 400, {"error": "empty message"})
            if state.get("kind") == "market" and state.get("assignment_id"):
                market = market_client()
                try:
                    market.message(state["assignment_id"], str(body)[:4000])
                except Exception as exc:
                    return _json(self, 502, {"error": str(exc)[:300]})
            state.setdefault("messages", []).append({"role": "buyer", "body": str(body)[:4000], "at": utcnow()})
            pipeline.save_job(state)
            if re.search(r"\b(?:file|image|video|voiceover|clip)\s*#?\s*\d+\b", body, re.I):
                enqueue("revision", {"assignment_id": state["job_id"], "feedback": body, "row": None})
            return _json(self, 200, {"ok": True})
        m = re.match(r"^/v1/jobs/([^/]+)/revise$", path)
        if m:
            if not _direct_authorized(self):
                return _json(self, 401, {"error": "unauthorized"})
            state = pipeline.load_job(m.group(1))
            if not state:
                return _json(self, 404, {"error": "job not found"})
            body = _read_json(self)
            feedback = str(body.get("feedback") or body.get("body") or "")
            if not feedback:
                return _json(self, 400, {"error": "empty feedback"})
            enqueue("revision", {"assignment_id": state["job_id"], "feedback": feedback, "row": None})
            return _json(self, 202, {"ok": True, "job_id": state["job_id"]})
        return _json(self, 404, {"error": "not found"})

    def _webhook(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        raw = self.rfile.read(length) if length else b""
        if not _verify_webhook(self.headers, raw):
            return _json(self, 401, {"error": "bad signature"})
        delivery = str(self.headers.get("X-Market-Delivery") or "")
        if not remember_delivery(delivery):
            return _json(self, 200, {"ok": True, "duplicate": True})
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            payload = {}
        event = str(self.headers.get("X-Market-Event") or payload.get("event") or "")
        # Answer 2xx before working.
        _json(self, 200, {"ok": True})
        try:
            handle_webhook_event(event, payload)
        except Exception as exc:
            log(f"webhook handling failed: {exc}")

    def _direct_hire(self) -> None:
        if not _direct_authorized(self):
            return _json(self, 401, {"error": "unauthorized; use a hiring path on Agent Market or a valid X-Test-Key"})
        body = _read_json(self)
        if not str(body.get("brief") or body.get("prompt") or "").strip():
            return _json(self, 400, {"error": "brief is required"})
        rate_key = self.client_address[0] if self.client_address else "unknown"
        state = create_direct_job(body, kind="direct")
        base = public_base()
        _json(self, 202, {
            "job_id": state["job_id"], "status": "queued",
            "status_url": f"{base}/v1/jobs/{state['job_id']}",
            "report_url": f"{base}/v1/jobs/{state['job_id']}/report",
        })

    def _health(self) -> None:
        payload = {
            "ok": True, "service": "near-media-generator", "version": "1.0.0",
            "time": utcnow(),
            "agent_id": CFG.get("agent_id"), "handle": CFG.get("handle"),
            "listing": listing_url(), "public_base": public_base(),
            "providers": {
                "image": providers.image_provider_name(),
                "video": providers.video_provider_name(),
                "video_fallback": "Wikimedia Commons CC-licensed stock (only when allowed; labelled)",
                "voice": providers.voice_provider_name(),
            },
            "queue": _queue.qsize(), "active": sorted(ACTIVE),
            "jobs": len(pipeline.list_jobs(200)),
        }
        return _json(self, 200, payload)

    def _serve_file(self, state: dict[str, Any], name: str) -> None:
        if os.path.basename(name) != name:
            return _json(self, 400, {"error": "bad filename"})
        info = next((o for o in state.get("outputs", []) if o.get("filename") == name and o.get("status") == "ok"), None)
        if not info or not os.path.exists(info.get("path", "")):
            return _json(self, 404, {"error": "file not found"})
        path = info["path"]
        size = os.path.getsize(path)
        mime = {"image": "image/jpeg", "video": "video/mp4", "voiceover": "audio/mpeg"}.get(info["kind"], "application/octet-stream")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
        length = max(0, end - start + 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def recover_jobs() -> None:
    for state in pipeline.list_jobs(100):
        if state.get("status") in ("queued", "running", "uploading"):
            log(f"recovering job {state['job_id']}")
            with _state_lock:
                ACTIVE.add(f"direct:{state['job_id']}")
            _queue.put({"kind": "direct", "payload": {"job_id": state["job_id"]}})


def main() -> None:
    port = int(os.environ.get("PORT") or CFG.get("port") or 8790)
    threading.Thread(target=worker_loop, daemon=True, name="worker").start()
    threading.Thread(target=poller_loop, daemon=True, name="poller").start()
    if CFG.get("build_job_id"):
        threading.Thread(target=build_watcher_loop, daemon=True, name="build-watcher").start()
    recover_jobs()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"listening on :{port}; public={public_base() or '(no tunnel yet)'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
