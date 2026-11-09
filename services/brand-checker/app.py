"""Self-hosted Brand Name Availability Checker for NEAR Agent Market.

Surfaces:
  * A public HTML form and JSON API for direct checks.
  * The Agent Market signed webhook + worker poller (start/progress/submit).
  * A build-job watcher for the existing pending bid on job e01a39f1.
  * A2A agent card and a small JSON-RPC endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import html
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

import checker
from market import MarketAPI, MarketError, OwnerAPI

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
SECRETS_PATH = os.path.join(ROOT, "secrets.json")
LOG_DIR = os.path.join(ROOT, "logs")
STATE_DIR = os.path.join(ROOT, "state")
JOBS_DIR = os.path.join(STATE_DIR, "jobs")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(JOBS_DIR, exist_ok=True)

START_TIME = time.time()
_log_lock = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def reload_config() -> None:
    global CFG, SECRETS
    CFG = load_json(CONFIG_PATH, {})
    SECRETS = load_json(SECRETS_PATH, {})


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
    return str(CFG.get("public_base_url") or os.environ.get("PUBLIC_BASE_URL") or "http://127.0.0.1:8791").rstrip("/")


def listing_url() -> str:
    handle = CFG.get("handle") or "brandnamechecker"
    return CFG.get("listing_url") or f"https://market.near.ai/a/{handle}"


def market_client() -> MarketAPI | None:
    token = SECRETS.get("agent_token")
    return MarketAPI(token) if token else None


# ---------------------------------------------------------------------------
# Durable jobs


_jobs_lock = threading.Lock()


def job_path(job_id: str) -> str:
    return os.path.join(JOBS_DIR, f"{job_id}.json")


def save_job(state: dict[str, Any]) -> None:
    state["updated_at"] = utcnow()
    path = job_path(state["job_id"])
    tmp = path + ".tmp"
    with _jobs_lock:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1)
        os.replace(tmp, path)


def load_job(job_id: str) -> dict[str, Any] | None:
    try:
        with open(job_path(job_id), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def list_jobs(limit: int = 40) -> list[dict[str, Any]]:
    rows = []
    try:
        for name in os.listdir(JOBS_DIR):
            if name.endswith(".json"):
                state = load_job(name[:-5])
                if state:
                    rows.append(state)
    except Exception:
        pass
    rows.sort(key=lambda s: s.get("created_at") or "", reverse=True)
    return rows[:limit]


def _job_summary(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": state.get("job_id"),
        "status": state.get("status"),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "kind": state.get("kind"),
        "names": (state.get("request") or {}).get("names"),
        "title": state.get("title"),
        "error": state.get("error"),
        "report_url": f"{public_base()}/r/{state.get('job_id')}",
        "result_url": f"{public_base()}/v1/jobs/{state.get('job_id')}/result.json",
    }


# ---------------------------------------------------------------------------
# Worker


WORK_QUEUE: "queue.Queue[tuple[str, str]]" = queue.Queue()
ACTIVE_LOCK = threading.Lock()
ACTIVE: set[str] = set()
PROGRESS_SENT: dict[str, float] = {}


def enqueue(kind: str, job_id: str) -> bool:
    key = f"{kind}:{job_id}"
    with ACTIVE_LOCK:
        if key in ACTIVE:
            return False
        ACTIVE.add(key)
    WORK_QUEUE.put((kind, job_id))
    return True


def _progress(market: MarketAPI | None, aid: str | None, pct: int, msg: str) -> None:
    if not market or not aid:
        return
    now = time.time()
    if now - PROGRESS_SENT.get(aid, 0) < 25:
        return
    PROGRESS_SENT[aid] = now
    try:
        market.progress(aid, msg, pct)
    except Exception:
        pass


def _market_message(market: MarketAPI | None, aid: str | None, body: str) -> None:
    if not market or not aid:
        return
    try:
        market.message(aid, body[:4000])
    except Exception as exc:
        log(f"message failed for {aid}: {exc}")


def _submit_job(state: dict[str, Any], market: MarketAPI, aid: str) -> None:
    report = state.get("report_md") or ""
    result = state.get("result") or {}
    files: list[str] = []
    try:
        json_path = os.path.join(JOBS_DIR, f"{state['job_id']}.json.result")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=1)
        uploaded = market.upload_file(json_path, "application/json")
        if uploaded.get("fileId"):
            files.append(uploaded["fileId"])
    except Exception as exc:
        log(f"JSON upload failed for {aid}: {exc}")
    header = (
        f"# {state.get('title') or 'Brand name availability report'}\n\n"
        f"Full report below. The same data is attached as JSON when the upload succeeded.\n\n"
        f"Job: `{state['job_id']}` · Finished: {state.get('finished_at')}\n\n---\n\n"
    )
    try:
        market.submit(aid, header + report, files=files)
        state["status"] = "submitted"
        state["submitted_at"] = utcnow()
        state["submission_files"] = files
        save_job(state)
        log(f"submitted market job {aid} with {len(files)} file(s)")
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"submit failed: {exc}"[:1000]
        save_job(state)
        log(f"submit failed for {aid}: {exc}")


def process_job(job_id: str, market: MarketAPI | None = None, assignment_id: str | None = None) -> None:
    state = load_job(job_id)
    if not state:
        return
    if state.get("status") in ("submitted", "complete") and not assignment_id:
        return
    state["status"] = "running"
    state["started_at"] = utcnow()
    save_job(state)
    try:
        request = state.get("request") or {}
        if not request.get("names"):
            raise ValueError("no candidate names in the request")
        _progress(market, assignment_id, 10, "confirming scope and starting live lookups")
        result = checker.run_check(request)
        report = checker.build_report(result)
        state["result"] = result
        state["report_md"] = report
        state["finished_at"] = result["finished_at"]
        state["status"] = "complete"
        save_job(state)
        _progress(market, assignment_id, 90, "lookups complete; writing the report")
        if market and assignment_id:
            _submit_job(state, market, assignment_id)
        else:
            log(f"completed direct job {job_id}")
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = str(exc)[:1500]
        state["traceback"] = traceback.format_exc()[-2000:]
        save_job(state)
        log(f"job {job_id} failed: {exc}")
        if market and assignment_id:
            _market_message(
                market,
                assignment_id,
                "I could not complete the check: "
                + str(exc)[:700]
                + "\n\nPlease send the candidate name(s) and any extensions/platforms/countries you want. "
                "Nothing was submitted.",
            )
    finally:
        with ACTIVE_LOCK:
            ACTIVE.discard(f"process:{job_id}")


def _request_from_market(row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    job = row.get("job") or {}
    latest = row.get("latestMessage") or {}
    description = str(job.get("description") or "")
    latest_body = str(latest.get("body") or "")
    if latest_body:
        # The buyer's own message is the authoritative brief.  Only fall back
        # to the job description for explicit "names:" lines, never by
        # scanning prose (the template itself contains plenty of English).
        request = checker.parse_request(latest_body)
        if request.get("names"):
            return latest_body + "\n\nJob description:\n" + description, request
        request = checker.parse_request(description, allow_line_scan=False)
        # A platform job description is not the buyer's scope statement.
        request["domains"] = list(checker.DEFAULT_DOMAINS)
        request["platforms"] = list(checker.DEFAULT_PLATFORMS)
        request["countries"] = list(checker.DEFAULT_COUNTRIES)
        request["variations"] = False
        return latest_body + "\n\nJob description:\n" + description, request
    request = checker.parse_request(description, allow_line_scan=False)
    request["domains"] = list(checker.DEFAULT_DOMAINS)
    request["platforms"] = list(checker.DEFAULT_PLATFORMS)
    request["countries"] = list(checker.DEFAULT_COUNTRIES)
    request["variations"] = False
    return description, request


_ASKED: dict[str, str] = {}


def handle_market_assignment(row: dict[str, Any], market: MarketAPI) -> None:
    aid = (row.get("assignment") or {}).get("assignmentId")
    if not aid:
        return
    key = f"assignment:{aid}"
    with ACTIVE_LOCK:
        if key in ACTIVE:
            return
        ACTIVE.add(key)
    try:
        _handle_market_assignment_inner(row, market)
    finally:
        with ACTIVE_LOCK:
            ACTIVE.discard(key)


def _handle_market_assignment_inner(row: dict[str, Any], market: MarketAPI) -> None:
    a = row.get("assignment") or {}
    job = row.get("job") or {}
    aid = a.get("assignmentId")
    if not aid:
        return
    brief, request = _request_from_market(row)
    if not request.get("names"):
        latest = str((row.get("latestMessage") or {}).get("body") or "")
        marker = hashlib.sha256((latest + "\n" + str(job.get("description") or "")).encode()).hexdigest()
        if _ASKED.get(aid) != marker:
            _ASKED[aid] = marker
            _market_message(
                market,
                aid,
                "Please send at least one candidate name. You can also say which domain extensions, "
                "social platforms and trademark countries you want; otherwise I use "
                + ", ".join(checker.DEFAULT_DOMAINS)
                + " and "
                + ", ".join(checker.PLATFORM_LABELS[p] for p in checker.DEFAULT_PLATFORMS)
                + ", with US trademarks.",
            )
        return
    _ASKED.pop(aid, None)
    job_id = str(uuid.uuid4())
    state = {
        "job_id": job_id,
        "kind": "market",
        "assignment_id": aid,
        "market_job_id": job.get("jobId"),
        "title": job.get("title") or f"Brand check: {', '.join(request['names'])}",
        "brief": brief,
        "request": request,
        "created_at": utcnow(),
        "status": "queued",
    }
    save_job(state)
    try:
        market.start(aid)
    except MarketError as exc:
        if exc.status not in (400, 409):
            log(f"start failed for {aid}: {exc}")
    _market_message(
        market,
        aid,
        "Checking "
        + ", ".join(request["names"])
        + " on "
        + ", ".join(request["domains"])
        + "; platforms: "
        + ", ".join(checker.PLATFORM_LABELS[p] for p in request["platforms"])
        + "; trademark countries: "
        + ", ".join(request["countries"])
        + ". I will submit the report here.",
    )
    process_job(job_id, market=market, assignment_id=aid)


def poll_own_assignments() -> None:
    market = market_client()
    if not market:
        return
    try:
        rows = market.get_assignments()
    except Exception as exc:
        log(f"assignment poll failed: {exc}")
        return
    for row in rows:
        a = row.get("assignment") or {}
        aid = a.get("assignmentId")
        if not aid:
            continue
        state_name = str(a.get("status") or "")
        if state_name in ("accepted", "refunded", "canceled", "cancelled"):
            continue
        local = None
        for job in list_jobs(100):
            if job.get("assignment_id") == aid:
                local = job
                break
        if local and local.get("status") in ("submitted", "complete"):
            continue
        if not a.get("startedAt"):
            try:
                handle_market_assignment(row, market)
            except Exception as exc:
                log(f"assignment {aid} failed: {exc}")
        elif local and local.get("status") == "failed":
            continue
        elif a.get("submittedAt") and a.get("status") == "in_progress":
            # Buyer asked for changes: rebuild from the latest brief.
            try:
                handle_market_assignment(row, market)
            except Exception as exc:
                log(f"rework {aid} failed: {exc}")


def poller_loop() -> None:
    while True:
        try:
            poll_own_assignments()
        except Exception:
            log("poller error: " + traceback.format_exc()[-800:])
        time.sleep(int(CFG.get("poll_seconds") or 30))


# ---------------------------------------------------------------------------
# Build-job watcher for the existing bid on job e01a39f1


_build_seen: dict[str, str] = {}
_build_last: dict[str, float] = {}


def _build_signature(row: dict[str, Any]) -> str:
    a = row.get("assignment") or {}
    return f"{a.get('assignmentId')}:{a.get('submittedAt')}:{a.get('status')}"


def build_job_markdown() -> str:
    live_cfg = load_json(CONFIG_PATH, CFG)
    test_job = live_cfg.get("test_job_id") or ""
    base = public_base()
    test_line = ""
    if test_job:
        test_line = (
            f"- Live public test report: {base}/r/{test_job}\n"
            f"- Test JSON: {base}/v1/jobs/{test_job}/result.json\n"
        )
    return f"""# Brand Name Availability Checker — live listing

**Listing:** {listing_url()}

**Agent ID:** `{CFG.get('agent_id') or 'see listing'}`  
**Category:** Research  
**Runtime:** self-hosted HTTP (`http`) with a signed Agent Market webhook plus polling fallback  
**Price:** {CFG.get('price') or '5.00'} USD per job  
**Delivery SLA:** {int(CFG.get('sla_seconds') or 3600) // 3600} hour(s)  
**Webhook:** `{base}/webhook`  
**Agent card:** `{base}/.well-known/agent-card.json`

## What it does

The listed agent takes one to five candidate names and checks them in one report:

- **Domains** on the requested extensions (default `.com`, `.io`, `.ai`, `.co`) through registry RDAP and registry WHOIS.
- **Social handles** on X, GitHub, YouTube, TikTok and Instagram. Each result is available, taken, or "couldn't confirm" with the reason. Instagram does not expose a public availability signal, so it is always reported as couldn't confirm rather than guessed.
- **App stores**: exact-name apps in the Apple App Store (iTunes Search API) and Google Play.
- **Trademarks**: live US federal marks from the USPTO Trademark Search index, and other requested countries through TMview (EUIPO/TMDN), with registration/application number, status, owner and similarity.
- **Suggested variations** when requested, checked on the requested domains and every platform that exposes a public signal.

Every row carries its own UTC check time and evidence URL. The report says clearly that trademark results are a starting point, not legal advice.

## Acceptance criteria mapping

1. **Every domain result matches a registrar lookup:** RDAP is the primary source and registry WHOIS confirms both available and taken results where the registry publishes WHOIS.
2. **Every platform result matches a manual check or says it couldn't confirm:** only public, observable signals are used. X (404 vs profile), GitHub API, YouTube, and TikTok oEmbed are confirmable. Instagram is explicitly "couldn't confirm".
3. **Apps with the exact same name:** Apple iTunes Search API and the public Google Play search page, filtered to exact normalized title matches.
4. **Trademarks with number, status and named database:** USPTO live marks with registration/application number and status; TMview for other offices.
5. **Every result shows when it was checked:** each row has its own `checked_at` UTC timestamp.
6. **Variations free on all domains and platforms:** generated variations are only marked confirmed when every checked domain and every checkable platform is available. Instagram is excluded and the report says so.
7. **No registration:** the agent has no registration, purchase or reservation code path. It only performs read-only lookups.
8. **Not legal advice:** the disclaimer is on every report and in the listing.

## Protocol endpoints on this host

| Purpose | Endpoint |
|---|---|
| Health | `GET /healthz` |
| Agent card | `GET /.well-known/agent-card.json` |
| Public form | `GET /` |
| JSON check API | `POST /v1/check` |
| Job status | `GET /v1/jobs/{{job_id}}` |
| Human report | `GET /r/{{job_id}}` |
| Machine result | `GET /v1/jobs/{{job_id}}/result.json` |
| Signed marketplace webhook | `POST /webhook` |

The production path is the Agent Market worker protocol: the marketplace posts signed `hire.created` events to `/webhook`, and the service also polls `GET /v1/agents/me/assignments` every 30 seconds. It calls `start`, posts progress, uploads a JSON result, and submits the Markdown report through `POST /v1/assignments/{{id}}/submit`.

## Test evidence

{test_line}
- The same checker code produced the public test report above from live RDAP/WHOIS, platform and trademark lookups.
- The listing is live and the webhook answered the platform's `webhook.ping`.

## Limits

- Availability changes constantly; every result is only valid at its stated check time.
- Instagram, Facebook, Reddit and Threads do not expose a reliable public handle-availability signal, so they are reported as couldn't confirm when requested.
- Text similarity is not a legal similarity judgment; the report flags possible conflicts only.
- The agent never registers, reserves or buys a domain or handle.

---

Built and operated by the owner of this listing. The agent is live and ready for acceptance testing.
"""


def build_watcher_loop() -> None:
    token_file = str(CFG.get("bid_agent_token_file") or os.environ.get("NEAR_MARKET_CREDS", "near_market.json"))
    job_id = str(CFG.get("bid_job_id") or "")
    if not job_id:
        return
    log(f"build watcher started for bid job {job_id}")
    while True:
        try:
            creds = load_json(token_file, {})
            token = creds.get("agent_token")
            if not token:
                log("build watcher: no agent token; stopping")
                return
            market = MarketAPI(token)
            for row in market.get_assignments():
                job = row.get("job") or {}
                a = row.get("assignment") or {}
                if str(job.get("jobId")) != job_id:
                    continue
                aid = a.get("assignmentId")
                if not aid:
                    continue
                if a.get("status") in ("accepted", "refunded", "canceled", "cancelled"):
                    continue
                if a.get("submittedAt"):
                    continue
                sig = _build_signature(row)
                if _build_seen.get(aid) == sig:
                    continue
                if time.time() - _build_last.get(aid, 0) < 120:
                    continue
                _build_last[aid] = time.time()
                log(f"build watcher processing assignment {aid}")
                try:
                    market.start(aid)
                except MarketError as exc:
                    if exc.status not in (400, 409):
                        log(f"build start failed: {exc}")
                _market_message(
                    market,
                    aid,
                    f"The Brand Name Availability Checker is live at {listing_url()}. "
                    "I am submitting the listing URL, coverage, acceptance mapping and public test evidence now.",
                )
                try:
                    result = market.submit(aid, build_job_markdown(), files=[])
                    _build_seen[aid] = sig
                    log(f"build deliverable submitted for {aid}: {result}")
                except Exception as exc:
                    log(f"build submit failed for {aid}: {exc}")
        except Exception as exc:
            log(f"build watcher error: {exc}")
        time.sleep(30)


# ---------------------------------------------------------------------------
# Webhook


def verify_webhook(headers: Any, raw: bytes) -> bool:
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


def handle_webhook(event: str, payload: dict[str, Any]) -> None:
    log(f"webhook {event} assignment={payload.get('assignment_id', '')}")
    if event == "webhook.ping":
        return
    if event == "hire.created":
        row = {
            "assignment": {
                "assignmentId": payload.get("assignment_id"),
                "jobId": payload.get("job_id"),
                "status": "in_progress",
                "startedAt": None,
                "submittedAt": None,
            },
            "job": {
                "jobId": payload.get("job_id"),
                "title": payload.get("title"),
                "description": payload.get("description"),
            },
            "latestMessage": None,
        }
        market = market_client()
        if market:
            handle_market_assignment(row, market)
    elif event in ("hire.changes_requested", "message.created"):
        aid = payload.get("assignment_id")
        market = market_client()
        if aid and market:
            try:
                for row in market.get_assignments():
                    if (row.get("assignment") or {}).get("assignmentId") == aid:
                        handle_market_assignment(row, market)
                        break
            except Exception as exc:
                log(f"webhook follow-up failed: {exc}")


# ---------------------------------------------------------------------------
# HTML


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{color-scheme:light dark}}*{{box-sizing:border-box}}body{{margin:0;font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif;background:#0b1020;color:#e8ecff}}
main{{max-width:960px;margin:0 auto;padding:32px 20px 64px}}h1{{font-size:1.8rem;margin:0 0 6px}}h2{{margin-top:28px;font-size:1.2rem}}
p.lead{{color:#a9b4d8;margin:0 0 22px}}card,form{{display:block;background:#141a30;border:1px solid #263057;border-radius:14px;padding:20px;margin:16px 0}}
label{{display:block;font-weight:600;margin:12px 0 4px}}input[type=text],select{{width:100%;padding:10px 12px;border-radius:9px;border:1px solid #33406e;background:#0d1226;color:#e8ecff}}
.row{{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center}}.row label{{font-weight:400;margin:0}}
button{{margin-top:16px;padding:11px 18px;border:0;border-radius:9px;background:#5b7cff;color:#fff;font-weight:700;cursor:pointer}}
button:disabled{{opacity:.55;cursor:wait}}pre{{white-space:pre-wrap;word-break:break-word;background:#0d1226;border:1px solid #263057;border-radius:12px;padding:16px;overflow:auto}}
a{{color:#8fb0ff}}code{{background:#0d1226;padding:1px 5px;border-radius:5px}}.muted{{color:#8d99bf;font-size:.9rem}}
.ok{{color:#7ee2a8}}.bad{{color:#ff9b9b}}.unk{{color:#ffd27a}}
</style></head><body><main>
<h1>Brand Name Availability Checker</h1>
<p class="lead">Domains, social handles, app stores and trademarks in one report — with a check time and source for every row.</p>
{body}
<p class="muted">Trademark results are a starting point, not legal advice. This agent never registers, reserves or buys a name.</p>
</main></body></html>"""


def index_page() -> str:
    doms = "".join(
        f'<label><input type="checkbox" name="dom" value="{d}" checked> {d}</label>'
        for d in checker.DEFAULT_DOMAINS
    )
    plats = "".join(
        f'<label><input type="checkbox" name="plat" value="{p}" checked> {checker.PLATFORM_LABELS[p]}</label>'
        for p in checker.DEFAULT_PLATFORMS
    )
    body = f"""
<form id="f">
<label for="names">Candidate names (1–5, comma separated)</label>
<input id="names" type="text" placeholder="acme, acmelabs" required>
<label>Domain extensions</label><div class="row">{doms}</div>
<label>Platforms</label><div class="row">{plats}</div>
<label for="countries">Trademark countries (ISO codes, comma separated)</label>
<input id="countries" type="text" value="US">
<div class="row" style="margin-top:12px"><label><input id="variations" type="checkbox"> Suggest variations</label></div>
<button id="go" type="submit">Run live checks</button>
<p class="muted" id="status">Checks are live and can take 20–60 seconds.</p>
</form>
<div id="out"></div>
<script>
const f=document.getElementById('f'), go=document.getElementById('go'), status=document.getElementById('status'), out=document.getElementById('out');
f.addEventListener('submit', async (e)=>{{
 e.preventDefault(); out.innerHTML=''; go.disabled=true; status.textContent='Running live lookups…';
 const names=document.getElementById('names').value.split(',').map(s=>s.trim()).filter(Boolean);
 const domains=[...document.querySelectorAll('input[name=dom]:checked')].map(x=>x.value);
 const plats=[...document.querySelectorAll('input[name=plat]:checked')].map(x=>x.value);
 const countries=document.getElementById('countries').value.split(',').map(s=>s.trim()).filter(Boolean);
 try {{
  const r=await fetch('/v1/check',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{names,domains,platforms:plats,countries,variations:document.getElementById('variations').checked}})}});
  const d=await r.json();
  if(!r.ok) throw new Error(d.error||('HTTP '+r.status));
  status.textContent='Done.';
  out.innerHTML='<card><h2>Report ready</h2><p><a href="'+d.report_url+'">Open the full report</a> · <a href="'+d.result_url+'">JSON result</a></p><pre></pre></card>';
  out.querySelector('pre').textContent=d.report_md||'';
 }} catch(err) {{ status.textContent=''; out.innerHTML='<card><p class="bad">'+err.message+'</p></card>'; }}
 finally {{ go.disabled=false; }}
}});
</script>"""
    return page("Brand Name Availability Checker", body)


def report_page(state: dict[str, Any]) -> str:
    report = html.escape(state.get("report_md") or state.get("error") or "No report yet.")
    body = f"""
<card>
<p><strong>Status:</strong> {html.escape(str(state.get('status')))} · <strong>Job:</strong> <code>{html.escape(state.get('job_id',''))}</code></p>
<p><a href="/v1/jobs/{html.escape(state.get('job_id',''))}/result.json">Download JSON result</a> · <a href="/">New check</a></p>
</card>
<pre>{report}</pre>"""
    return page(f"Report {state.get('job_id','')[:8]}", body)


# ---------------------------------------------------------------------------
# HTTP handler


_rate_lock = threading.Lock()
_rate: dict[str, list[float]] = {}


def rate_ok(ip: str, per_minute: int = 8) -> bool:
    now = time.time()
    with _rate_lock:
        q = [t for t in _rate.get(ip, []) if now - t < 60]
        if len(q) >= per_minute:
            _rate[ip] = q
            return False
        q.append(now)
        _rate[ip] = q
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "BrandNameChecker/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _path(self) -> str:
        return urllib.parse.urlparse(self.path).path.rstrip("/") or "/"

    def do_GET(self) -> None:
        path = self._path()
        try:
            if path in ("/", "/index.html"):
                return _send(self, 200, index_page(), "text/html; charset=utf-8")
            if path == "/healthz":
                return _json(self, 200, {
                    "ok": True,
                    "service": "brand-name-checker",
                    "uptime_seconds": int(time.time() - START_TIME),
                    "listing": listing_url(),
                    "public_base": public_base(),
                })
            if path in ("/.well-known/agent-card.json", "/agent-card"):
                return _json(self, 200, agent_card())
            if path == "/v1/jobs":
                return _json(self, 200, {"jobs": [_job_summary(s) for s in list_jobs()]})
            if path.startswith("/r/"):
                state = load_job(path[len("/r/"):])
                if not state:
                    return _json(self, 404, {"error": "job not found"})
                return _send(self, 200, report_page(state), "text/html; charset=utf-8")
            if path.startswith("/v1/jobs/"):
                parts = path[len("/v1/jobs/"):].split("/")
                state = load_job(parts[0])
                if not state:
                    return _json(self, 404, {"error": "job not found"})
                if len(parts) == 1:
                    return _json(self, 200, _job_summary(state))
                if len(parts) == 2 and parts[1] == "report":
                    return _send(self, 200, state.get("report_md") or "Report not ready.", "text/markdown; charset=utf-8")
                if len(parts) == 2 and parts[1] == "result.json":
                    return _json(self, 200, state.get("result") or {})
            return _json(self, 404, {"error": "not found", "path": path})
        except Exception as exc:
            log("GET error: " + traceback.format_exc()[-800:])
            return _json(self, 500, {"error": str(exc)[:400]})

    def do_POST(self) -> None:
        path = self._path()
        ip = self.client_address[0]
        try:
            if path == "/webhook":
                return self._webhook()
            if path == "/v1/check":
                if not rate_ok(ip, 6):
                    return _json(self, 429, {"error": "too many checks from this address; try again in a minute"})
                body = _read_json(self)
                body.setdefault("names", body.get("name"))
                request = checker.parse_request(json.dumps(body))
                if not request.get("names"):
                    return _json(self, 400, {"error": "At least one candidate name is required."})
                job_id = str(uuid.uuid4())
                state = {
                    "job_id": job_id,
                    "kind": "direct",
                    "title": "Brand check: " + ", ".join(request["names"]),
                    "request": request,
                    "created_at": utcnow(),
                    "status": "queued",
                }
                save_job(state)
                process_job(job_id)
                state = load_job(job_id) or state
                return _json(self, 200 if state.get("status") == "complete" else 500, {
                    "job_id": job_id,
                    "status": state.get("status"),
                    "error": state.get("error"),
                    "report_url": f"{public_base()}/r/{job_id}",
                    "result_url": f"{public_base()}/v1/jobs/{job_id}/result.json",
                    "report_md": state.get("report_md") or "",
                    "result": state.get("result") or {},
                })
            if path == "/v1/hire":
                body = _read_json(self)
                request = checker.parse_request(json.dumps(body))
                if not request.get("names"):
                    return _json(self, 400, {"error": "At least one candidate name is required."})
                job_id = str(uuid.uuid4())
                state = {
                    "job_id": job_id,
                    "kind": "direct",
                    "title": "Brand check: " + ", ".join(request["names"]),
                    "request": request,
                    "created_at": utcnow(),
                    "status": "queued",
                }
                save_job(state)
                enqueue("process", job_id)
                return _json(self, 202, {"job_id": job_id, "status": "queued",
                                         "report_url": f"{public_base()}/r/{job_id}"})
            return _json(self, 404, {"error": "not found", "path": path})
        except Exception as exc:
            log("POST error: " + traceback.format_exc()[-800:])
            return _json(self, 500, {"error": str(exc)[:400]})

    def _webhook(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not verify_webhook(self.headers, raw):
            return _json(self, 401, {"error": "invalid signature"})
        event = self.headers.get("X-Market-Event") or ""
        try:
            payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except Exception:
            return _json(self, 400, {"error": "invalid JSON"})
        _json(self, 200, {"ok": True})
        threading.Thread(target=handle_webhook, args=(event, payload), daemon=True).start()


def _read_json(handler: Handler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        raise ValueError(f"invalid JSON: {exc}")
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def _send(handler: Handler, status: int, body: str, content_type: str) -> None:
    raw = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(raw)


def _json(handler: Handler, status: int, payload: Any) -> None:
    _send(handler, status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")


def agent_card() -> dict[str, Any]:
    base = public_base()
    return {
        "protocolVersion": "0.3.0",
        "name": "Brand Name Availability Checker",
        "description": "Checks candidate names across registry domains, public social handles, app stores and trademark databases, with a timestamp and source per result.",
        "url": f"{base}/",
        "version": "1.0.0",
        "provider": {"organization": "Brand Name Availability Checker"},
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["text/markdown", "application/json"],
        "skills": [{
            "id": "brand-name-check",
            "name": "Brand name availability check",
            "description": "Domains via RDAP/WHOIS, handles on X/GitHub/YouTube/TikTok/Instagram, exact-name apps on Apple and Google Play, and trademarks via USPTO and TMview.",
            "tags": ["brand", "naming", "domain", "trademark", "research"],
            "examples": ["Check the name acme on .com/.io/.ai, X/Instagram/TikTok and US trademarks."],
        }],
    }


# ---------------------------------------------------------------------------
# Entrypoint


def worker_loop() -> None:
    while True:
        kind, job_id = WORK_QUEUE.get()
        try:
            if kind == "process":
                process_job(job_id)
        except Exception:
            log("worker error: " + traceback.format_exc()[-1000:])
        finally:
            WORK_QUEUE.task_done()


def main() -> None:
    port = int(CFG.get("port") or os.environ.get("PORT") or 8791)
    reload_config()
    threading.Thread(target=worker_loop, daemon=True, name="worker").start()
    if SECRETS.get("agent_token"):
        threading.Thread(target=poller_loop, daemon=True, name="poller").start()
    if CFG.get("bid_job_id") and CFG.get("bid_agent_token_file", os.environ.get("NEAR_MARKET_CREDS", "near_market.json")):
        threading.Thread(target=build_watcher_loop, daemon=True, name="build-watcher").start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log(f"listening on 0.0.0.0:{port} public={public_base()} listing={listing_url()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
