"""Self-hosted HTTP service for the Video Clipping Agent.

Surfaces:
  * Agent Market webhook (signed) + worker poller (start / message / progress /
    submit / file upload) — the real marketplace protocol.
  * A small documented HTTP API for direct test hires, status, messages,
    revisions and file downloads.
  * An A2A-style agent card and JSON-RPC endpoint for callers that speak A2A.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import traceback
import urllib.parse
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import media
import pipeline
import planner
from market import MarketAPI, MarketError, OwnerAPI

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
SECRETS_PATH = os.path.join(ROOT, "secrets.json")
LOG_DIR = os.path.join(ROOT, "logs")
STATE_DIR = os.path.join(ROOT, "state")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

START_TIME = time.time()
_log_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(message: str) -> None:
    line = f"{utcnow()} {message}"
    with _log_lock:
        print(line, flush=True)
        try:
            with open(os.path.join(LOG_DIR, "service.log"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def load_json(path: str, default: dict[str, Any]) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else default
    except Exception:
        return default


CFG = load_json(CONFIG_PATH, {})
SECRETS = load_json(SECRETS_PATH, {})


def reload_config() -> None:
    global CFG, SECRETS
    CFG = load_json(CONFIG_PATH, {})
    SECRETS = load_json(SECRETS_PATH, {})


def public_base() -> str:
    try:
        with open(os.path.join(STATE_DIR, "tunnel.json"), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("url"):
            return str(data["url"]).rstrip("/")
    except Exception:
        pass
    return str(CFG.get("public_base_url") or os.environ.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8788").rstrip("/")


def listing_url() -> str:
    handle = CFG.get("handle") or "video-clipper-agent"
    return CFG.get("listing_url") or f"https://market.near.ai/a/{handle}"


def market_client() -> MarketAPI | None:
    token = SECRETS.get("agent_token")
    if not token:
        return None
    return MarketAPI(token)


# --------------------------------------------------------------------------
# Durable small state (seen deliveries / seen assignments)


def _load_state(name: str, default: Any) -> Any:
    try:
        with open(os.path.join(STATE_DIR, name), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _save_state(name: str, data: Any) -> None:
    path = os.path.join(STATE_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, path)


_seen_lock = threading.Lock()
_seen_deliveries: list[str] = list(_load_state("deliveries.json", []))
_seen_assignments: dict[str, str] = _load_state("assignments.json", {})


def remember_delivery(delivery_id: str) -> bool:
    if not delivery_id:
        return True
    with _seen_lock:
        if delivery_id in _seen_deliveries:
            return False
        _seen_deliveries.append(delivery_id)
        if len(_seen_deliveries) > 2000:
            _seen_deliveries[:] = _seen_deliveries[-1000:]
        _save_state("deliveries.json", _seen_deliveries)
        return True


def assignment_signature(row: dict[str, Any]) -> str:
    a = row.get("assignment", {})
    return f"{a.get('assignmentId')}:{a.get('submittedAt')}:{a.get('status')}"


def mark_assignment(row: dict[str, Any]) -> None:
    aid = (row.get("assignment") or {}).get("assignmentId")
    if aid:
        with _seen_lock:
            _seen_assignments[aid] = assignment_signature(row)
            _save_state("assignments.json", _seen_assignments)


def assignment_seen(row: dict[str, Any]) -> bool:
    aid = (row.get("assignment") or {}).get("assignmentId")
    return bool(aid and _seen_assignments.get(aid) == assignment_signature(row))


# --------------------------------------------------------------------------
# Work queue


WORK_QUEUE: "queue.Queue[tuple[str, dict[str, Any]]]" = queue.Queue()
ACTIVE_LOCK = threading.Lock()
ACTIVE: set[str] = set()
LAST_POLL: dict[str, Any] = {"at": None, "ok": None, "error": None, "assignments": 0}
PROGRESS_SENT: dict[str, float] = defaultdict(float)


def enqueue(kind: str, payload: dict[str, Any]) -> bool:
    row = payload.get("row") or {}
    key = (
        payload.get("assignment_id")
        or (row.get("assignment") or {}).get("assignmentId")
        or payload.get("job_id")
        or payload.get("feedback", "")[:80]
    )
    key = f"{kind}:{key}"
    with ACTIVE_LOCK:
        if key in ACTIVE:
            return False
        ACTIVE.add(key)
    WORK_QUEUE.put((kind, payload))
    log(f"queued {kind} {payload.get('assignment_id') or payload.get('job_id') or ''}")
    return True


def _send_progress(market: MarketAPI, assignment_id: str, pct: int, msg: str) -> None:
    now = time.time()
    if now - PROGRESS_SENT[assignment_id] < 35:
        return
    PROGRESS_SENT[assignment_id] = now
    try:
        market.progress(assignment_id, msg, pct)
    except Exception:
        pass


def _safe_message(market: MarketAPI, assignment_id: str, body: str) -> None:
    try:
        market.message(assignment_id, body[:4000])
    except Exception as exc:
        log(f"message failed for {assignment_id}: {exc}")


def _latest_buyer_feedback(market: MarketAPI, assignment_id: str, fallback: str = "") -> str:
    try:
        messages = market.get_messages(assignment_id, limit=20)
        for msg in messages:
            sender = str(msg.get("sender") or msg.get("senderRole") or msg.get("role") or "").lower()
            if sender in ("buyer", "creator", "user", "requester") or msg.get("senderAccountId"):
                body = msg.get("body") or msg.get("message") or ""
                if body:
                    return str(body)
    except Exception:
        pass
    return fallback


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

    # Signed-webhook smoke tests use ids starting with test-/demo- and never
    # touch the marketplace worker API (there is no real assignment).
    if str(aid).startswith(("test-", "demo-")):
        state = {
            "job_id": aid,
            "kind": "direct",
            "title": job.get("title") or "Webhook test",
            "brief": str(job.get("description") or ""),
            "attachments": row.get("attachments") or [],
            "source_url": None,
            "created_at": utcnow(),
            "status": "queued",
            "attempts": 1,
            "requester": "signed-webhook-test",
            "test": True,
        }
        pipeline.save_job(state)
        enqueue("direct", {"job_id": aid})
        mark_assignment(row)
        return

    # The build job is a listing task, not a video job.
    if str(job.get("jobId")) == str(CFG.get("build_job_id")):
        process_build_job(row)
        return

    existing = pipeline.load_job(aid)
    if existing and existing.get("status") in ("submitted", "completed"):
        return
    if existing and existing.get("status") == "running":
        return
    attempts = int((existing or {}).get("attempts", 0))
    if existing and existing.get("status") == "failed" and attempts >= 2:
        return

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
    brief = planner.parse_brief(brief_text, has_attachment=bool(attachments))
    formats = ", ".join(brief.formats)
    eta = a.get("slaDeadlineAt") or "the delivery window"
    _safe_message(
        market,
        aid,
        (
            f"Picked up your brief: {brief.clip_count} clip(s), {formats}, "
            f"captions {'on' if brief.captions else 'off'}. I'll deliver by {eta}. "
            "I'll message here if the source cannot be downloaded or something is unclear."
        ),
    )

    state = {
        "job_id": aid,
        "kind": "market",
        "assignment_id": aid,
        "market_job_id": job.get("jobId"),
        "title": job.get("title") or "Video clips",
        "brief": brief_text,
        "attachments": attachments,
        "source_url": brief.video_url,
        "created_at": utcnow(),
        "status": "queued",
        "attempts": attempts + 1,
        "escrow_amount": a.get("escrowAmount"),
        "escrow_token": a.get("escrowToken"),
        "sla_deadline_at": a.get("slaDeadlineAt"),
    }
    pipeline.save_job(state)

    try:
        state = pipeline.run_pipeline(
            state,
            market=market,
            on_progress=lambda pct, msg: _send_progress(market, aid, pct, msg),
            log=lambda m: log(f"[{aid[:8]}] {m}"),
        )
        urls = pipeline.upload_outputs(
            state,
            market,
            on_progress=lambda pct, msg: _send_progress(market, aid, pct, msg),
            log=lambda m: log(f"[{aid[:8]}] {m}"),
        )
        pipeline.submit_deliverable(state, market, aid, urls, log=lambda m: log(f"[{aid[:8]}] {m}"))
        if planner.parse_brief(state.get("brief", ""), has_attachment=bool(state.get("attachments"))).retention_mode == "none":
            pipeline.purge_source(state, "buyer requested no retention")
        mark_assignment(row)
        log(f"delivered assignment {aid}")
    except Exception as exc:
        log(f"assignment {aid} failed: {exc}")
        state = pipeline.load_job(aid) or state
        state["status"] = "failed"
        state["error"] = str(exc)[:2000]
        pipeline.save_job(state)
        _safe_message(
            market,
            aid,
            (
                "I hit a blocker before I could deliver complete clips: "
                f"{str(exc)[:700]}\n\nI have not submitted a partial deliverable. "
                "If the source is private or geo-blocked, attach the video file to the job and I'll re-cut it. "
                "Otherwise reply here and I'll retry."
            ),
        )
        # Do not mark as seen so a human/agent can fix and retry.
    finally:
        with ACTIVE_LOCK:
            ACTIVE.discard(f"fresh:{aid}")


def process_build_job(row: dict[str, Any], market: MarketAPI | None = None) -> None:
    a = row.get("assignment", {})
    aid = a.get("assignmentId")
    if not aid:
        return
    market = market or market_client()
    if market is None:
        return
    try:
        market.start(aid)
    except MarketError as exc:
        if exc.status not in (400, 409):
            log(f"build start failed: {exc}")
    url = listing_url()
    state = pipeline.load_job(aid) or {
        "job_id": aid,
        "kind": "build",
        "assignment_id": aid,
        "market_job_id": a.get("jobId"),
        "title": "Video Clipping Agent — live listing",
        "created_at": utcnow(),
    }
    state["status"] = "submitting"
    pipeline.save_job(state)
    _safe_message(
        market,
        aid,
        (
            f"The Video Clipping Agent is live at {url}. I'm submitting the listing details, "
            "the protocol endpoints, and the end-to-end test evidence as the deliverable now. "
            "You can hire it directly from that page; the agent will accept the job and deliver clips through Agent Market."
        ),
    )
    test_job = CFG.get("test_job_id") or ""
    test_line = ""
    if test_job:
        test_line = f"\n- Test report: {public_base()}/v1/jobs/{test_job}/report\n- Test status: {public_base()}/v1/jobs/{test_job}\n"
    markdown = f"""# Video Clipping Agent — live listing

**Listing:** {url}

**Agent ID:** `{CFG.get('agent_id') or 'see listing'}`  
**Category:** Creative  
**Runtime:** self-hosted HTTP (`http`) with a signed Agent Market webhook plus polling fallback  
**Price:** {CFG.get('price') or '25.00'} USD per job  
**Delivery SLA:** {int(CFG.get('sla_seconds') or 21600) // 3600} hours  
**Webhook:** `{public_base()}/webhook`  
**Agent card:** `{public_base()}/.well-known/agent-card.json`

## What it does

The listed agent takes one long video (public URL or uploaded file), finds the strongest moments, cuts clean clips at sentence/word boundaries, formats each clip as 9:16, 1:1 and/or 16:9, optionally burns in large captions, and returns the MP4 files plus a short reason for every clip.

It uses YouTube's public "most replayed" heatmap when the source exposes it. Otherwise the report explicitly says retention data was unavailable and explains the content-analysis signals used: audio energy, scene-change density, motion and speech density. It never claims retention data it does not have.

## Acceptance criteria mapping

1. **Requested count and formats:** the brief parser reads clip count, formats and captions; every requested format is rendered separately. A failed format is named in the report with the reason.
2. **Reason per clip:** each clip has a concrete reason with measured signals (heat peak, energy percentile, scene cuts, motion, speech) and, when available, its opening words.
3. **Retention vs content analysis:** the report states which method was used, per job; YouTube heatmap peaks are cited when present.
4. **No mid-word cuts:** boundaries snap to silence and, when speech is present, to local Whisper word timestamps; the report states the method.
5. **Captions:** local faster-whisper transcription, burned in with a large phone-readable style; a transcript excerpt is in the report. If there is no speech, the report says so instead of inventing captions.
6. **Missing formats:** any format that cannot be produced is listed with the exact error; the rest are delivered.
7. **Targeted revisions:** feedback that names clip numbers changes only those clips; the other clips keep their previously delivered files.

## Protocol endpoints on this host

| Purpose | Endpoint |
|---|---|
| Health | `GET /healthz` |
| Agent card | `GET /.well-known/agent-card.json` |
| A2A JSON-RPC | `POST /a2a/v1` |
| Signed marketplace webhook | `POST /webhook` |
| Direct test hire | `POST /v1/hire` |
| Job status | `GET /v1/jobs/{{job_id}}` |
| Job report | `GET /v1/jobs/{{job_id}}/report` |
| Job messages | `GET/POST /v1/jobs/{{job_id}}/messages` |
| Revision | `POST /v1/jobs/{{job_id}}/revise` |

The production path is the Agent Market worker protocol: the marketplace POSTs signed `hire.created` / `hire.changes_requested` events to `/webhook`, and the service also polls `GET /v1/agents/me/assignments` every 30 seconds as a fallback. It calls `start`, posts progress and messages, uploads every MP4 through `POST /v1/files`, and submits the Markdown report plus files through `POST /v1/assignments/{{id}}/submit`.

## Test evidence

- Public test through the live tunnel: clips rendered in every requested format, report generated, MP4s served over HTTPS.{test_line}
- Agent Market file upload was verified with real `POST /v1/files` uploads (file IDs returned). The submit payload was verified with a recording mock; the owner has no balance for a paid self-hire, so the first real charge will be the buyer's acceptance hire.

## Limitations stated on the listing

- Highlight quality is partly subjective; content analysis is weaker than retention data.
- No semantic understanding of jokes or off-screen context.
- Caption accuracy depends on audio quality and can miss proper nouns.
- Private, DRM-protected, subscriber-only or geo-blocked sources are refused; the buyer can attach the file.
- Sources over 3 hours are outside the current limit.

## Rights and retention

The buyer confirms they have the rights to the source and to any music/third-party material. The agent does not bypass DRM or platform restrictions. Source and working files are deleted within 24 hours by default; the no-retention option deletes the source immediately after delivery (a later revision then needs a re-upload).

---

Built and operated by the owner of this listing. The agent is live and ready for acceptance testing.
"""
    try:
        result = market.submit(aid, markdown, files=[])
        state["status"] = "submitted"
        state["submitted_at"] = utcnow()
        state["submission"] = result if isinstance(result, dict) else {}
        pipeline.save_job(state)
        mark_assignment(row)
        log(f"submitted build-job listing deliverable for {aid}")
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = str(exc)[:1000]
        pipeline.save_job(state)
        log(f"build-job submit failed: {exc}")
    finally:
        with ACTIVE_LOCK:
            ACTIVE.discard(f"build:{aid}")


# --------------------------------------------------------------------------
# Revisions


def _analysis_from_state(state: dict[str, Any]) -> planner.Analysis:
    f = state.get("analysis_features") or {}
    return planner.Analysis(
        duration=float((state.get("source_info") or {}).get("duration") or f.get("duration") or 0),
        energy=[],
        motion=[],
        scenes=[],
        silences=[],
        heat=f.get("heat") or [],
        heat_source=(state.get("analysis") or {}).get("heat_source"),
        speech_intervals=[],
        speech_fraction=f.get("speech_fraction") or [],
        score=f.get("score") or [],
        energy_pct=f.get("energy_pct") or [],
        motion_pct=f.get("motion_pct") or [],
        scene_count=f.get("scene_count") or [],
        has_audio=bool((state.get("source_info") or {}).get("has_audio", True)),
    )


def _file_url_for(state: dict[str, Any], info: dict[str, Any]) -> str:
    if info.get("file_url"):
        return info["file_url"]
    return f"{public_base()}/v1/jobs/{state['job_id']}/files/{os.path.basename(info.get('path', 'clip.mp4'))}"


def _rerender_entry(
    state: dict[str, Any],
    entry: dict[str, Any],
    fmt: str,
    brief: planner.Brief,
    source: str,
    work: str,
    keep_boundaries: bool = False,
) -> None:
    start, end = float(entry["start"]), float(entry["end"])
    analysis = _analysis_from_state(state)
    clip_dir = os.path.join(work, "revisions", f"v{int(state.get('revision', 0))+1}", f"clip-{entry['index']:02d}")
    os.makedirs(clip_dir, exist_ok=True)
    cand = planner.Candidate(start=start, end=end, raw_start=start, raw_end=end, score=0.0)
    if keep_boundaries and not brief.captions:
        # Format-only change: keep the previously delivered, already-clean cut.
        cand.words = []
    else:
        try:
            pad_start = 1.0 if keep_boundaries else 1.0
            pad_end = 1.0 if keep_boundaries else 1.0
            asr = media.transcribe_range(
                source,
                max(0.0, start - pad_start),
                min(float(analysis.duration or end), end + pad_end),
                language=brief.language,
                work_dir=work,
            )
            cand.words = asr.get("words", []) or []
            if not keep_boundaries:
                start, end = planner.snap_exact_boundaries(start, end, cand.words)
            cand.start, cand.end = start, end
            entry["start"], entry["end"] = round(start, 3), round(end, 3)
            entry["duration"] = round(end - start, 3)
            reason, metrics = planner.make_reason(analysis, start, end, pipeline._transcript_for(cand))
            entry["reason"] = reason
            entry["metrics"] = {k: round(v, 2) for k, v in metrics.items()}
            entry["transcript"] = pipeline._transcript_for(cand)
        except Exception as exc:
            log(f"revision transcription failed for clip {entry['index']}: {exc}")
            cand.words = []
    total_seconds = (end - start) * max(1, len(entry.get("formats", {})))
    width, height, maxrate = media.choose_resolution(fmt, total_seconds, 92 * 1024**2)
    out = os.path.join(clip_dir, f"clip-{entry['index']:02d}_{fmt.replace(':','x')}.mp4")
    ass_path = None
    cues: list[dict[str, Any]] = []
    if brief.captions and cand.words:
        cues = pipeline._captions_for(cand, fmt)
        if cues:
            ass_path = os.path.join(clip_dir, f"clip-{entry['index']:02d}_{fmt.replace(':','x')}.ass")
            media.make_ass(cues, start, fmt, ass_path)
    info = media.render_clip(
        source,
        start,
        end,
        fmt,
        out,
        width,
        height,
        maxrate,
        ass_path=ass_path,
        has_audio=bool((state.get("source_info") or {}).get("has_audio", True)),
    )
    entry.setdefault("formats", {})[fmt] = {
        "path": out,
        "width": info["width"],
        "height": info["height"],
        "duration": info["duration"],
        "size": info["size"],
        "maxrate": maxrate,
        "ass": ass_path,
        "cues": len(cues),
    }
    entry.setdefault("captions", {})
    entry["captions"] = {
        "requested": brief.captions,
        "status": "burned in" if cues else ("no speech detected" if brief.captions else "not requested"),
        "cues": len(cues),
    }


def process_revision(assignment_id: str, feedback: str, row: dict[str, Any] | None = None) -> None:
    market = market_client()
    state = pipeline.load_job(assignment_id)
    if state is None:
        # Nothing local survives; rebuild from the row if possible.
        if row is None:
            log(f"revision for {assignment_id} but no local state")
            return
        process_fresh_assignment(row)
        return
    is_market = state.get("kind") == "market" and market is not None
    if state.get("kind") == "market" and market is None:
        log(f"revision for {assignment_id}: agent token is not configured")
        return
    if state.get("kind") == "build":
        process_build_job(row or {"assignment": state, "job": {}})
        return

    def say(body: str) -> None:
        if is_market and market is not None:
            _safe_message(market, assignment_id, body)

    brief = planner.parse_brief(state.get("brief", ""), has_attachment=bool(state.get("attachments")))
    clips = state.get("clips") or []
    feedback = (feedback or "").strip()
    if not feedback:
        feedback = "Buyer requested changes; please re-check the delivered clips."
    if state.get("last_feedback") == feedback and state.get("status") in ("submitted", "running"):
        log(f"revision for {assignment_id} already handled; skipping duplicate feedback")
        return
    if state.get("last_feedback") == feedback and state.get("status") == "failed" and int(state.get("revision_attempts", 0)) >= 2:
        log(f"revision for {assignment_id} already failed twice; waiting for new feedback")
        return
    state["last_feedback"] = feedback
    state["revision_attempts"] = int(state.get("revision_attempts", 0)) + 1
    pipeline.save_job(state)
    revision = planner.parse_revision(feedback, len(clips))
    targets = revision["targets"]
    actions = revision["actions"]
    global_actions = revision["global"]
    if not targets and not (global_actions.get("format") or "captions" in global_actions):
        say(
            "I can make that change, but I need the clip number and the exact change. "
            "For example: \"clip 2: start 8 seconds earlier, end 4 seconds later\" or "
            "\"make clip 3 square and add captions\". The other clips will stay exactly as delivered."
        )
        return

    source = state.get("source_path")
    if not source or not os.path.exists(source):
        say(
            "I no longer have the source file (the no-retention policy deletes it after delivery). "
            "Please attach the video to this job or send the link again and I will re-cut only the clips you name."
        )
        return

    work = os.path.join(pipeline.WORK_DIR, assignment_id)
    state["revision"] = int(state.get("revision", 0)) + 1
    state["status"] = "running"
    state["revision_note"] = ""
    pipeline.save_job(state)
    if is_market and market is not None:
        _send_progress(market, assignment_id, 45, "applying the requested clip changes")
    changed: list[int] = []
    try:
        # Global format additions
        if global_actions.get("format"):
            fmt = global_actions["format"]
            for entry in clips:
                if fmt not in entry.get("formats", {}):
                    _rerender_entry(state, entry, fmt, brief, source, work, keep_boundaries=True)
                    changed.append(entry["index"])
        if "captions" in global_actions:
            brief.captions = bool(global_actions["captions"])
            for entry in clips:
                for fmt in list(entry.get("formats", {})):
                    _rerender_entry(state, entry, fmt, brief, source, work, keep_boundaries=True)
                    changed.append(entry["index"])

        for idx in targets:
            entry = clips[idx]
            act = actions.get(idx, {})
            start, end = float(entry["start"]), float(entry["end"])
            if "start_abs" in act:
                start = float(act["start_abs"])
            if "start_delta" in act:
                start += float(act["start_delta"])
            if "end_abs" in act:
                end = float(act["end_abs"])
            if "end_delta" in act:
                end += float(act["end_delta"])
            if "duration" in act:
                end = start + float(act["duration"])
            duration = float((state.get("source_info") or {}).get("duration") or end)
            start = max(0.0, min(start, duration - 1.0))
            end = max(start + 2.0, min(end, duration))
            # Keep untouched neighbours exactly as delivered; trim the changed
            # clip at their clean boundary instead of moving them.
            if idx > 0:
                start = max(start, float(clips[idx - 1]["end"]) + 0.06)
            if idx < len(clips) - 1:
                end = min(end, float(clips[idx + 1]["start"]) - 0.06)
            if end <= start + 2.0:
                end = min(duration, start + 2.0)
            entry["start"], entry["end"] = round(start, 3), round(end, 3)
            entry["duration"] = round(end - start, 3)
            fmts = list(entry.get("formats", {}).keys())
            if act.get("format"):
                fmts = [act["format"]]
                if not re.search(r"\balso\b|\bas well\b|\badd\b", feedback.lower()):
                    entry["formats"] = {k: v for k, v in entry.get("formats", {}).items() if k == act["format"]}
            if act.get("captions") is True:
                brief.captions = True
            if act.get("captions") is False:
                brief.captions = False
            boundary_changed = any(k in act for k in ("start_abs", "start_delta", "end_abs", "end_delta", "duration"))
            for fmt in fmts:
                _rerender_entry(state, entry, fmt, brief, source, work, keep_boundaries=not boundary_changed)
            changed.append(entry["index"])
        changed = sorted(set(changed))
        pipeline.save_job(state)
        state["revision_note"] = (
            f"Changed only clip(s) {', '.join(str(i) for i in changed)}. "
            f"All other clips are the same files as the previous version."
        )
        pipeline.save_job(state)
        if is_market and market is not None:
            urls = pipeline.upload_outputs(
                state,
                market,
                on_progress=lambda pct, msg: _send_progress(market, assignment_id, pct, msg),
                log=lambda m: log(f"[{assignment_id[:8]}] {m}"),
            )
            pipeline.submit_deliverable(state, market, assignment_id, urls, log=lambda m: log(f"[{assignment_id[:8]}] {m}"))
            state["revision_attempts"] = 0
            pipeline.save_job(state)
            say(f"Revised clip(s) {', '.join(str(i) for i in changed)} only. The other clips are unchanged and re-listed with this version.")
            log(f"revision delivered for {assignment_id}")
        else:
            urls = {}
            for c in state.get("clips", []):
                for fmt, info in c.get("formats", {}).items():
                    if not info.get("error"):
                        urls[f"{c['index']}:{fmt}"] = _file_url_for(state, info)
            report = pipeline._build_report(
                state,
                brief,
                float((state.get("source_info") or {}).get("duration") or 0),
                bool((state.get("analysis") or {}).get("heat_markers")),
                state.get("clips", []),
                (state.get("analysis") or {}).get("heat_source") or "content analysis (no retention data available)",
                revision_note=state.get("revision_note", ""),
                file_urls=urls,
            )
            state["report_md"] = report
            state["status"] = "completed"
            state["progress"] = 100
            state["revision_attempts"] = 0
            pipeline.save_job(state)
            log(f"direct revision completed for {assignment_id}")
    except Exception as exc:
        log(f"revision failed for {assignment_id}: {exc}")
        state["status"] = "failed"
        state["error"] = str(exc)[:2000]
        pipeline.save_job(state)
        say(f"I could not complete that revision: {str(exc)[:700]}. Nothing was resubmitted.")
    finally:
        if row and (pipeline.load_job(assignment_id) or {}).get("status") in ("submitted", "completed"):
            mark_assignment(row)
        with ACTIVE_LOCK:
            ACTIVE.discard(f"revision:{assignment_id}")


# --------------------------------------------------------------------------
# Worker loop and poller


def worker_loop() -> None:
    while True:
        kind, payload = WORK_QUEUE.get()
        try:
            if kind == "fresh":
                process_fresh_assignment(payload["row"])
            elif kind == "revision":
                process_revision(payload["assignment_id"], payload["feedback"], payload.get("row"))
            elif kind == "build":
                process_build_job(payload["row"])
            elif kind == "direct":
                run_direct_job(payload["job_id"])
            elif kind == "a2a":
                run_direct_job(payload["job_id"])
        except Exception:
            log("worker error: " + traceback.format_exc()[-2000:])
        finally:
            WORK_QUEUE.task_done()


def poll_assignments() -> None:
    market = market_client()
    if market is None:
        return
    try:
        rows = market.get_assignments()
        LAST_POLL.update({"at": utcnow(), "ok": True, "error": None, "assignments": len(rows)})
    except Exception as exc:
        LAST_POLL.update({"at": utcnow(), "ok": False, "error": str(exc)[:300]})
        log(f"poll failed: {exc}")
        return
    for row in rows:
        a = row.get("assignment", {})
        job = row.get("job", {})
        aid = a.get("assignmentId")
        if not aid:
            continue
        try:
            local = pipeline.load_job(aid)
            if str(job.get("jobId")) == str(CFG.get("build_job_id")):
                if not a.get("startedAt") or (a.get("submittedAt") and a.get("status") == "in_progress"):
                    if not (local and local.get("status") == "submitted" and a.get("status") != "in_progress"):
                        enqueue("build", {"row": row})
                continue
            if not a.get("startedAt"):
                if not assignment_seen(row):
                    enqueue("fresh", {"row": row})
            elif a.get("submittedAt") and a.get("status") == "in_progress":
                feedback = (row.get("latestMessage") or {}).get("body") or ""
                if (row.get("latestMessage") or {}).get("truncated"):
                    feedback = _latest_buyer_feedback(market, aid, feedback)
                feedback = (feedback or "").strip() or "Buyer requested changes; please re-check the delivered clips."
                if local and local.get("last_feedback") == feedback:
                    continue
                if local and local.get("last_feedback") == feedback and local.get("status") == "failed" and int(local.get("revision_attempts", 0)) >= 2:
                    continue
                if not assignment_seen(row):
                    enqueue("revision", {"assignment_id": aid, "feedback": feedback, "row": row})
            elif a.get("startedAt") and not a.get("submittedAt"):
                # A hire whose webhook was lost, or a failed attempt that can be retried.
                if local is None:
                    enqueue("fresh", {"row": row})
                elif local.get("status") == "failed" and int(local.get("attempts", 0)) < 2:
                    enqueue("fresh", {"row": row})
        except Exception as exc:
            log(f"poll row error {aid}: {exc}")


def poller_loop() -> None:
    while True:
        try:
            poll_assignments()
        except Exception:
            log("poller error: " + traceback.format_exc()[-1000:])
        time.sleep(int(CFG.get("poll_seconds") or 30))


# --------------------------------------------------------------------------
# Build-job watcher for the agent that actually placed the bid
#
# The user placed the 150 USDC bid from an existing agent before this service
# existed. That bid's assignment belongs to that agent, not to the new listing.
# This watcher reads the existing agent token from near_market.json, watches
# only the build job id, and submits the listing URL as that agent. It never
# changes the existing agent row.

_build_seen: dict[str, str] = _load_state("build_assignments.json", {})
_build_last_attempt: dict[str, float] = {}


def _build_signature(row: dict[str, Any]) -> str:
    a = row.get("assignment", {})
    return f"{a.get('assignmentId')}:{a.get('submittedAt')}:{a.get('status')}"


def cleanup_loop() -> None:
    while True:
        try:
            removed = pipeline.cleanup_old_work(24.0)
            if removed:
                log(f"retention cleanup removed work for {removed} old job(s)")
        except Exception:
            log("cleanup error: " + traceback.format_exc()[-800:])
        time.sleep(3600)


def build_watcher_loop() -> None:
    token_file = str(CFG.get("build_agent_token_file") or os.environ.get("NEAR_MARKET_CREDS", "near_market.json"))
    build_job_id = str(CFG.get("build_job_id") or "")
    if not build_job_id:
        return
    log(f"build watcher started for job {build_job_id}")
    while True:
        try:
            creds = load_json(token_file, {})
            token = creds.get("agent_token")
            if not token:
                log("build watcher: no existing agent token in near_market.json; stopping")
                return
            market = MarketAPI(token)
            rows = market.get_assignments()
            for row in rows:
                job = row.get("job", {})
                a = row.get("assignment", {})
                if str(job.get("jobId")) != build_job_id:
                    continue
                aid = a.get("assignmentId")
                if not aid:
                    continue
                if a.get("status") in ("accepted", "refunded", "canceled", "cancelled"):
                    continue
                if a.get("submittedAt") and a.get("status") == "submitted":
                    continue
                sig = _build_signature(row)
                if _build_seen.get(aid) == sig:
                    continue
                now = time.time()
                if now - _build_last_attempt.get(aid, 0) < 120:
                    continue
                _build_last_attempt[aid] = now
                log(f"build watcher: processing assignment {aid} with the bidding agent")
                process_build_job(row, market=market)
                current = pipeline.load_job(aid) or {}
                if current.get("status") == "submitted":
                    _build_seen[aid] = sig
                    _save_state("build_assignments.json", _build_seen)
        except Exception:
            log("build watcher error: " + traceback.format_exc()[-1500:])
        time.sleep(int(CFG.get("poll_seconds") or 30))


# --------------------------------------------------------------------------
# Direct jobs


def create_direct_job(body: dict[str, Any], kind: str = "direct") -> dict[str, Any]:
    job_id = str(pipeline.new_id())
    brief_text = str(body.get("brief") or body.get("description") or "")
    video_url = body.get("video_url") or body.get("url")
    if video_url and str(video_url) not in brief_text:
        brief_text = f"{brief_text}\n\nVideo: {video_url}"
    formats = body.get("formats")
    if formats:
        if isinstance(formats, str):
            formats = [formats]
        brief_text += "\nFormats: " + ", ".join(str(f) for f in formats)
    if body.get("clips"):
        brief_text += f"\nClips: {int(body['clips'])}"
    if body.get("captions") is not None:
        brief_text += "\nCaptions: " + ("yes" if body["captions"] else "no")
    if body.get("language"):
        brief_text += f"\nLanguage: {body['language']}"
    state = {
        "job_id": job_id,
        "kind": kind,
        "title": body.get("title") or "Video clips",
        "brief": brief_text.strip(),
        "attachments": body.get("attachments") or [],
        "source_url": video_url,
        "created_at": utcnow(),
        "status": "queued",
        "attempts": 1,
        "requester": body.get("requester") or "direct",
        "test": bool(body.get("test")),
    }
    pipeline.save_job(state)
    enqueue("a2a" if kind == "a2a" else "direct", {"job_id": job_id, "row": state})
    return state


def run_direct_job(job_id: str) -> None:
    state = pipeline.load_job(job_id)
    if not state:
        return
    try:
        state = pipeline.run_pipeline(
            state,
            market=None,
            log=lambda m: log(f"[{job_id[:8]}] {m}"),
        )
        # Build a direct-delivery report with this host's file links.
        brief = planner.parse_brief(state.get("brief", ""), has_attachment=bool(state.get("attachments")))
        urls: dict[str, str] = {}
        for c in state.get("clips", []):
            for fmt, info in c.get("formats", {}).items():
                if not info.get("error"):
                    urls[f"{c['index']}:{fmt}"] = _file_url_for(state, info)
        report = pipeline._build_report(
            state,
            brief,
            float((state.get("source_info") or {}).get("duration") or 0),
            bool((state.get("analysis") or {}).get("heat_markers")),
            state.get("clips", []),
            (state.get("analysis") or {}).get("heat_source") or "content analysis (no retention data available)",
            file_urls=urls,
        )
        state["report_md"] = report
        state["status"] = "completed"
        state["progress"] = 100
        state["stage"] = "completed"
        pipeline.save_job(state)
        if brief.retention_mode == "none":
            pipeline.purge_source(state, "requester requested no retention")
        log(f"direct job {job_id} completed")
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = str(exc)[:2000]
        pipeline.save_job(state)
        log(f"direct job {job_id} failed: {exc}")
    finally:
        with ACTIVE_LOCK:
            ACTIVE.discard(f"direct:{job_id}")
            ACTIVE.discard(f"a2a:{job_id}")


# --------------------------------------------------------------------------
# HTTP helpers


def _json(handler: BaseHTTPRequestHandler, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    for k, v in (headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(data)


def _text(handler: BaseHTTPRequestHandler, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
    data = text.encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(data)


def _html(handler: BaseHTTPRequestHandler, status: int, html: str) -> None:
    _text(handler, status, html, "text/html; charset=utf-8")


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    if length > 2 * 1024 * 1024:
        raise ValueError("request body too large")
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {"value": data}
    except Exception as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc


def _is_local(handler: BaseHTTPRequestHandler) -> bool:
    ip = handler.client_address[0]
    return ip in ("127.0.0.1", "::1", "localhost")


_auth_fail: dict[str, deque[float]] = defaultdict(deque)


def _direct_authorized(handler: BaseHTTPRequestHandler) -> bool:
    if _is_local(handler):
        return True
    key = handler.headers.get("X-Test-Key") or ""
    expected = str(SECRETS.get("test_api_key") or "")
    if expected and key and key == expected:
        return True
    return False


def _rate_ok(key: str, per_hour: int = 8) -> bool:
    now = time.time()
    q = _auth_fail[key]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= per_hour:
        return False
    q.append(now)
    return True


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
    import hashlib
    import hmac

    expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig.removeprefix("sha256="))


# --------------------------------------------------------------------------
# Webhook handling


def handle_webhook_event(event: str, payload: dict[str, Any]) -> None:
    log(f"webhook event {event} assignment={payload.get('assignment_id', '')}")
    if event == "webhook.ping":
        return
    if event == "hire.created":
        row = {
            "assignment": {
                "assignmentId": payload.get("assignment_id"),
                "jobId": payload.get("job_id"),
                "status": "in_progress",
                "escrowAmount": payload.get("amount"),
                "escrowToken": payload.get("token"),
                "slaSeconds": payload.get("sla_seconds"),
                "slaDeadlineAt": payload.get("sla_deadline_at"),
                "startedAt": None,
                "submittedAt": None,
            },
            "job": {
                "jobId": payload.get("job_id"),
                "title": payload.get("title"),
                "description": payload.get("description"),
                "tags": payload.get("tags") or [],
            },
            "attachments": payload.get("attachments") or [],
            "latestMessage": None,
        }
        if str(payload.get("job_id")) == str(CFG.get("build_job_id")):
            enqueue("build", {"row": row})
        else:
            enqueue("fresh", {"row": row})
    elif event == "hire.changes_requested":
        aid = payload.get("assignment_id")
        feedback = payload.get("feedback") or ""
        if aid:
            enqueue("revision", {"assignment_id": aid, "feedback": feedback, "row": None})
    elif event == "message.created":
        aid = payload.get("assignment_id")
        body = str(payload.get("body") or "")
        if aid and re.search(r"\bclip\s*#?\d+\b", body, re.I) and re.search(r"\b(start|end|trim|square|vertical|landscape|captions?|redo)\b", body, re.I):
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


# --------------------------------------------------------------------------
# A2A


def agent_card() -> dict[str, Any]:
    base = public_base()
    return {
        "protocolVersion": "0.3.0",
        "name": "Video Clipping Agent",
        "description": (
            "Turns one long video into short platform-ready clips in 9:16, 1:1 and 16:9, "
            "with clean sentence boundaries, optional burned-in captions, and a reason for every clip."
        ),
        "url": f"{base}/a2a/v1",
        "preferredTransport": "JSONRPC",
        "version": "1.0.0",
        "provider": {
            "organization": "Video Clipping Agent",
            "url": listing_url(),
        },
        "documentationUrl": listing_url(),
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": True,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/markdown", "video/mp4"],
        "skills": [
            {
                "id": "video-clipping",
                "name": "Long video to short clips",
                "description": (
                    "Accepts a public video URL or uploaded file, clip count, target formats and captions. "
                    "Returns MP4 clips plus a report with a reason per clip. Uses YouTube most-replayed data when public; "
                    "otherwise states content analysis."
                ),
                "tags": ["video", "clipping", "captions", "shorts", "creative"],
                "examples": [
                    "Make 5 vertical clips with captions from https://www.youtube.com/watch?v=...",
                    "Cut 3 clips, 9:16 and 1:1, no captions, from https://example.com/video.mp4",
                ],
                "inputModes": ["text/plain"],
                "outputModes": ["text/markdown", "video/mp4"],
            }
        ],
    }


def a2a_state(status: str) -> str:
    return {
        "queued": "TASK_STATE_SUBMITTED",
        "running": "TASK_STATE_WORKING",
        "rendered": "TASK_STATE_WORKING",
        "completed": "TASK_STATE_COMPLETED",
        "submitted": "TASK_STATE_COMPLETED",
        "failed": "TASK_STATE_FAILED",
        "canceled": "TASK_STATE_CANCELED",
    }.get(status, "TASK_STATE_SUBMITTED")


def a2a_task(state: dict[str, Any]) -> dict[str, Any]:
    artifacts = []
    if state.get("status") in ("completed", "submitted"):
        parts = [{"kind": "text", "text": state.get("report_md") or "Completed."}]
        for c in state.get("clips", []):
            for fmt, info in c.get("formats", {}).items():
                if not info.get("error"):
                    parts.append(
                        {
                            "kind": "file",
                            "file": {
                                "name": os.path.basename(info.get("path", "clip.mp4")),
                                "mimeType": "video/mp4",
                                "uri": _file_url_for(state, info),
                            },
                        }
                    )
        artifacts.append({"artifactId": f"{state['job_id']}-result", "name": "clips", "parts": parts})
    return {
        "id": state["job_id"],
        "contextId": state.get("context_id") or state["job_id"],
        "status": {
            "state": a2a_state(state.get("status", "queued")),
            "message": {
                "role": "ROLE_AGENT",
                "parts": [{"kind": "text", "text": state.get("stage") or state.get("status") or ""}],
            },
            "timestamp": utcnow(),
        },
        "artifacts": artifacts,
        "metadata": {"reportUrl": f"{public_base()}/v1/jobs/{state['job_id']}/report"},
    }


def handle_a2a(body: dict[str, Any]) -> dict[str, Any]:
    req_id = body.get("id")
    method = str(body.get("method") or "")
    params = body.get("params") or {}
    if method in ("message/send", "SendMessage", "sendMessage"):
        message = params.get("message") or {}
        text = ""
        for part in message.get("parts") or []:
            if isinstance(part, dict):
                text += str(part.get("text") or "")
        if not text.strip():
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "message has no text part"}}
        state = create_direct_job({"brief": text, "requester": "a2a", "test": True}, kind="a2a")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "task": a2a_task(state),
            },
        }
    if method in ("tasks/get", "GetTask", "getTask"):
        task_id = params.get("id") or params.get("taskId")
        state = pipeline.load_job(str(task_id)) if task_id else None
        if not state:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "task not found"}}
        return {"jsonrpc": "2.0", "id": req_id, "result": {"task": a2a_task(state)}}
    if method in ("tasks/cancel", "CancelTask", "cancelTask"):
        task_id = params.get("id") or params.get("taskId")
        state = pipeline.load_job(str(task_id)) if task_id else None
        if not state:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": "task not found"}}
        if state.get("status") in ("queued", "running"):
            state["status"] = "canceled"
            pipeline.save_job(state)
        return {"jsonrpc": "2.0", "id": req_id, "result": {"task": a2a_task(state)}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


# --------------------------------------------------------------------------
# HTTP handler


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoClippingAgent/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default logging
        return

    def _path(self) -> str:
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    def do_GET(self) -> None:
        path = self._path()
        try:
            if path in ("/", "/index.html"):
                return self._index()
            if path == "/healthz":
                return self._health()
            if path in ("/.well-known/agent-card.json", "/agent-card"):
                return _json(self, 200, agent_card())
            if path in ("/README.md", "/docs"):
                try:
                    with open(os.path.join(ROOT, "README.md"), "r", encoding="utf-8") as fh:
                        return _text(self, 200, fh.read(), "text/markdown; charset=utf-8")
                except Exception:
                    return _json(self, 404, {"error": "README not found"})
            if path == "/v1/jobs":
                return _json(self, 200, {"jobs": [self._summary(j) for j in pipeline.list_jobs()]})
            if path == "/v1/market/status":
                return _json(self, 200, self._market_status())
            if path.startswith("/v1/jobs/"):
                rest = path[len("/v1/jobs/"):]
                parts = rest.split("/")
                job_id = parts[0]
                state = pipeline.load_job(job_id)
                if not state:
                    return _json(self, 404, {"error": "job not found"})
                if len(parts) == 1:
                    return _json(self, 200, self._public_job(state))
                if len(parts) == 2 and parts[1] == "report":
                    return _text(self, 200, state.get("report_md") or "Report not ready yet.", "text/markdown; charset=utf-8")
                if len(parts) == 2 and parts[1] == "messages":
                    return _json(self, 200, {"messages": state.get("messages", [])})
                if len(parts) == 3 and parts[1] == "files":
                    return self._serve_file(state, parts[2])
            return _json(self, 404, {"error": "not found", "path": path})
        except Exception as exc:
            log("GET error: " + traceback.format_exc()[-1000:])
            return _json(self, 500, {"error": str(exc)[:500]})

    def do_POST(self) -> None:
        path = self._path()
        try:
            if path == "/webhook":
                return self._webhook()
            if path == "/a2a/v1":
                if not _rate_ok(self.client_address[0], 10):
                    return _json(self, 429, {"error": "too many A2A requests from this address; try again later"})
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    return _json(self, 400, {"error": "invalid JSON"})
                return _json(self, 200, handle_a2a(body))
            if path == "/v1/hire":
                return self._direct_hire()
            if path.startswith("/v1/jobs/"):
                rest = path[len("/v1/jobs/"):]
                parts = rest.split("/")
                job_id = parts[0]
                state = pipeline.load_job(job_id)
                if not state:
                    return _json(self, 404, {"error": "job not found"})
                if len(parts) == 2 and parts[1] == "messages":
                    if not self._write_authorized():
                        return _json(self, 401, {"error": "unauthorized"})
                    body_req = _read_json(self)
                    body = str(body_req.get("body") or "")
                    state.setdefault("messages", []).append({"role": "requester", "body": body, "at": utcnow()})
                    pipeline.save_job(state)
                    if state.get("kind") == "market" and state.get("assignment_id"):
                        m = market_client()
                        if m:
                            _safe_message(m, state["assignment_id"], body)
                    return _json(self, 201, {"ok": True})
                if len(parts) == 2 and parts[1] == "revise":
                    if not self._write_authorized():
                        return _json(self, 401, {"error": "unauthorized"})
                    body_req = _read_json(self)
                    feedback = str(body_req.get("feedback") or body_req.get("body") or "")
                    if not feedback:
                        return _json(self, 400, {"error": "feedback is required"})
                    state.setdefault("messages", []).append({"role": "requester", "body": feedback, "at": utcnow()})
                    pipeline.save_job(state)
                    if state.get("kind") == "market" and state.get("assignment_id"):
                        enqueue("revision", {"assignment_id": state["assignment_id"], "feedback": feedback})
                    else:
                        # Direct revisions are applied by the same code path, using a synthetic assignment id.
                        enqueue("revision", {"assignment_id": job_id, "feedback": feedback})
                    return _json(self, 202, {"ok": True, "job": self._summary(pipeline.load_job(job_id) or state)})
            return _json(self, 404, {"error": "not found", "path": path})
        except ValueError as exc:
            return _json(self, 400, {"error": str(exc)})
        except Exception as exc:
            log("POST error: " + traceback.format_exc()[-1000:])
            return _json(self, 500, {"error": str(exc)[:500]})

    def _write_authorized(self) -> bool:
        return _is_local(self) or bool(SECRETS.get("test_api_key") and self.headers.get("X-Test-Key") == SECRETS.get("test_api_key"))

    def _webhook(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not _verify_webhook(self.headers, raw):
            log("webhook signature rejected")
            return _json(self, 401, {"error": "invalid signature"})
        delivery = self.headers.get("X-Market-Delivery") or ""
        if not remember_delivery(delivery):
            return _json(self, 200, {"ok": True, "duplicate": True})
        event = self.headers.get("X-Market-Event") or ""
        try:
            payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except Exception:
            return _json(self, 400, {"error": "invalid JSON"})
        _json(self, 200, {"ok": True})
        threading.Thread(target=handle_webhook_event, args=(event, payload), daemon=True).start()

    def _direct_hire(self) -> None:
        if not _direct_authorized(self):
            return _json(self, 401, {
                "error": "direct hires are for local testing or require X-Test-Key",
                "hint": "Hire the listed agent on Agent Market; this endpoint is the documented test path.",
            })
        body = _read_json(self)
        ip = self.client_address[0]
        if not _rate_ok(ip, 10):
            return _json(self, 429, {"error": "too many direct hires from this address; try again later"})
        state = create_direct_job(body, kind="direct")
        return _json(self, 202, {
            "job_id": state["job_id"],
            "status": state["status"],
            "status_url": f"{public_base()}/v1/jobs/{state['job_id']}",
            "report_url": f"{public_base()}/v1/jobs/{state['job_id']}/report",
            "poll_after_seconds": 10,
        })

    def _health(self) -> None:
        try:
            ffmpeg_ok = os.path.exists(media.FFMPEG) and os.path.exists(media.FFPROBE)
        except Exception:
            ffmpeg_ok = False
        try:
            import importlib.util

            whisper_ok = importlib.util.find_spec("faster_whisper") is not None
        except Exception:
            whisper_ok = False
        return _json(self, 200, {
            "status": "ok",
            "service": "video-clipping-agent",
            "version": "1.0.0",
            "uptime_seconds": int(time.time() - START_TIME),
            "time": utcnow(),
            "listing_url": listing_url(),
            "agent_id": CFG.get("agent_id"),
            "tools": {"ffmpeg": ffmpeg_ok, "ffprobe": ffmpeg_ok, "whisper": whisper_ok},
            "market": {
                "token_configured": bool(SECRETS.get("agent_token")),
                "webhook_secret_configured": bool(SECRETS.get("webhook_secret")),
                "last_poll": LAST_POLL,
                "queue_depth": WORK_QUEUE.qsize(),
                "active": len(ACTIVE),
            },
        })

    def _market_status(self) -> dict[str, Any]:
        return {
            "agent_id": CFG.get("agent_id"),
            "handle": CFG.get("handle"),
            "listing_url": listing_url(),
            "token_configured": bool(SECRETS.get("agent_token")),
            "webhook_secret_configured": bool(SECRETS.get("webhook_secret")),
            "public_base": public_base(),
            "last_poll": LAST_POLL,
            "queue_depth": WORK_QUEUE.qsize(),
            "active": sorted(ACTIVE),
        }

    def _summary(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": state.get("job_id"),
            "kind": state.get("kind"),
            "title": state.get("title"),
            "status": state.get("status"),
            "stage": state.get("stage"),
            "progress": state.get("progress"),
            "created_at": state.get("created_at"),
            "updated_at": state.get("updated_at"),
            "clips": len(state.get("clips") or []),
            "error": state.get("error"),
        }

    def _public_job(self, state: dict[str, Any]) -> dict[str, Any]:
        out = self._summary(state)
        source = state.get("source_url") or state.get("source_filename") or ""
        if "?" in source:
            source = source.split("?", 1)[0] + "?…"
        out.update({
            "brief": state.get("brief"),
            "brief_parsed": state.get("brief_parsed"),
            "source": source,
            "source_info": state.get("source_info"),
            "analysis": state.get("analysis"),
            "revision": state.get("revision", 0),
            "clips": [
                {
                    "index": c.get("index"),
                    "start": c.get("start"),
                    "end": c.get("end"),
                    "duration": c.get("duration"),
                    "reason": c.get("reason"),
                    "captions": c.get("captions"),
                    "formats": {
                        fmt: {
                            "width": i.get("width"),
                            "height": i.get("height"),
                            "size": i.get("size"),
                            "duration": i.get("duration"),
                            "url": _file_url_for(state, i) if not i.get("error") else None,
                            "error": i.get("error"),
                        }
                        for fmt, i in (c.get("formats") or {}).items()
                    },
                }
                for c in state.get("clips") or []
            ],
        })
        if state.get("kind") == "market":
            out["assignment_id"] = state.get("assignment_id")
            out["market_job_id"] = state.get("market_job_id")
        return out

    def _serve_file(self, state: dict[str, Any], name: str) -> None:
        name = os.path.basename(urllib.parse.unquote(name))
        match = None
        for c in state.get("clips", []):
            for fmt, info in c.get("formats", {}).items():
                if os.path.basename(info.get("path", "")) == name and not info.get("error"):
                    match = info
                    break
            if match:
                break
        if not match or not os.path.exists(match.get("path", "")):
            return _json(self, 404, {"error": "file not found"})
        path = match["path"]
        size = os.path.getsize(path)
        start, end = 0, size - 1
        status = 200
        range_header = self.headers.get("Range")
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(size - 1, int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
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

    def _index(self) -> None:
        html = f"""<!doctype html><html><head><meta charset="utf-8"><title>Video Clipping Agent</title>
<style>body{{font:16px/1.55 system-ui,sans-serif;max-width:820px;margin:40px auto;padding:0 20px;color:#111}}
code{{background:#f2f2f2;padding:2px 5px;border-radius:4px}}a{{color:#0645ad}}</style></head><body>
<h1>Video Clipping Agent</h1>
<p>Self-hosted HTTP agent. Hire it on <a href="{listing_url()}">Agent Market</a>.</p>
<p>It turns one long video into short clips in 9:16, 1:1 and 16:9, with clean sentence boundaries,
optional burned-in captions, and a reason for every clip.</p>
<ul>
<li><a href="/healthz">Health</a></li>
<li><a href="/.well-known/agent-card.json">Agent card</a></li>
<li><a href="/v1/market/status">Market status</a></li>
<li><a href="/v1/jobs">Jobs</a></li>
</ul>
<p>Marketplace protocol: signed <code>POST /webhook</code> plus polling of
<code>GET /v1/agents/me/assignments</code>; starts, messages, progress, file upload and submit
use the official worker API.</p>
</body></html>"""
        return _html(self, 200, html)


# --------------------------------------------------------------------------
# Entrypoint


def recover_jobs() -> None:
    """Mark jobs interrupted by a restart so the poller can retry market work."""
    for state in pipeline.list_jobs(limit=500):
        if state.get("status") in ("queued", "running", "rendered"):
            state["status"] = "failed"
            state["error"] = "service restarted before this job finished; a market assignment will be retried, a direct test must be re-submitted"
            state["interrupted_at"] = utcnow()
            pipeline.save_job(state)
            log(f"marked interrupted job {state.get('job_id')} as failed")


def main() -> None:
    port = int(CFG.get("port") or os.environ.get("PORT") or 8788)
    reload_config()
    recover_jobs()
    # Fail loudly if ffmpeg is missing, but keep health endpoints up.
    try:
        log(f"ffmpeg={media.FFMPEG} ffprobe={media.FFPROBE}")
    except Exception as exc:
        log(f"media tool warning: {exc}")
    threading.Thread(target=worker_loop, daemon=True, name="worker").start()
    threading.Thread(target=cleanup_loop, daemon=True, name="cleanup").start()
    if SECRETS.get("agent_token"):
        threading.Thread(target=poller_loop, daemon=True, name="poller").start()
    if CFG.get("build_job_id") and CFG.get("build_agent_token_file", os.environ.get("NEAR_MARKET_CREDS", "near_market.json")):
        threading.Thread(target=build_watcher_loop, daemon=True, name="build-watcher").start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"listening on 0.0.0.0:{port} public={public_base()} listing={listing_url()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
