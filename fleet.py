#!/usr/bin/env python3
"""NEAR Agent Fleet: verify, route to, and demo three paid agents.

The fleet is a small control plane over three self-hosted agents listed on
NEAR AI Agent Market. It has no third-party dependencies.

Commands:
    python3 fleet.py catalog
    python3 fleet.py check --json evidence/fleet-status.json --md evidence/fleet-status.md
    python3 fleet.py route "I need five vertical clips with captions from a podcast"
    python3 fleet.py serve --port 8780
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "fleet.json"
USER_AGENT = "near-agent-fleet/1.0 (+https://github.com/matrixneoaiagent/near-agent-fleet)"
MAX_BODY = 2 * 1024 * 1024


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_catalog(path: Path | str = CATALOG_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        catalog = json.load(fh)
    if not isinstance(catalog.get("agents"), list) or not catalog["agents"]:
        raise ValueError("fleet.json must contain a non-empty agents list")
    ids = [a.get("id") for a in catalog["agents"]]
    if len(ids) != len(set(ids)):
        raise ValueError("agent ids must be unique")
    for agent in catalog["agents"]:
        for key in ("id", "name", "listing", "price_usd", "sla", "health", "card"):
            if not agent.get(key):
                raise ValueError(f"agent {agent.get('id')!r} is missing {key}")
    return catalog


def http_request(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Return status, headers, body and latency. Never raises for HTTP errors."""
    data = None
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY)
            return {
                "ok": True,
                "status": resp.status,
                "content_type": resp.headers.get("Content-Type", ""),
                "body": body,
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_BODY) if exc.fp else b""
        return {
            "ok": False,
            "status": exc.code,
            "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
            "body": body,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:  # network, TLS, timeout
        return {
            "ok": False,
            "status": 0,
            "content_type": "",
            "body": b"",
            "latency_ms": int((time.monotonic() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def parse_listing_jsonld(document: str) -> dict[str, Any] | None:
    """Extract the schema.org Service object from an Agent Market listing."""
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        document,
        flags=re.S | re.I,
    )
    for block in blocks:
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "Service":
                offer = item.get("offers") or {}
                provider = item.get("provider") or {}
                return {
                    "name": str(item.get("name") or ""),
                    "description": str(item.get("description") or ""),
                    "price": str(offer.get("price") or ""),
                    "currency": str(offer.get("priceCurrency") or ""),
                    "service_type": str(item.get("serviceType") or ""),
                    "provider": str(provider.get("name") or ""),
                }
    return None


def parse_delivered_jobs(document: str) -> str | None:
    match = re.search(r"<dt>\s*Delivered jobs\s*</dt>\s*<dd>([^<]+)</dd>", document, flags=re.I)
    return html_lib.unescape(match.group(1)).strip() if match else None


def _check(
    check_id: str,
    label: str,
    url: str,
    ok: bool,
    detail: str,
    latency_ms: int = 0,
    http_status: int = 0,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "ok": bool(ok),
        "detail": detail,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "url": url,
    }


def check_agent(agent: dict[str, Any], timeout: float = 25.0) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    delivered: str | None = None

    listing = http_request(agent["listing"], timeout=timeout)
    service = None
    if listing["ok"]:
        document = listing["body"].decode("utf-8", "replace")
        service = parse_listing_jsonld(document)
        delivered = parse_delivered_jobs(document)
        if not service:
            detail = "listing returned 200 but no schema.org Service block"
            ok = False
        elif agent["name"].casefold() not in service["name"].casefold():
            detail = f"listing name mismatch: {service['name']!r}"
            ok = False
        elif service["price"] and service["price"] != agent["price_usd"]:
            detail = f"price mismatch: listing {service['price']} vs catalog {agent['price_usd']}"
            ok = False
        else:
            detail = f"{service['name']} / {service['service_type']} / ${service['price']} {service['currency']}"
            ok = True
    else:
        detail = listing["error"] or "listing unreachable"
        ok = False
    checks.append(
        _check("listing", "Public listing", agent["listing"], ok, detail, listing["latency_ms"], listing["status"])
    )

    health = http_request(agent["health"], timeout=timeout)
    health_data = _json(health["body"]) if health["ok"] else None
    health_ok = bool(
        health["ok"]
        and isinstance(health_data, dict)
        and (health_data.get("ok") is True or health_data.get("status") == "ok")
    )
    if health_ok:
        detail = (
            f"ok / {health_data.get('service', 'service')} / "
            f"up {health_data.get('uptime_seconds', '?')}s"
        )
    else:
        detail = (health["error"] or "health check failed") + (
            f" / {health_data}" if health_data is not None else ""
        )
    checks.append(
        _check("health", "Health endpoint", agent["health"], health_ok, detail, health["latency_ms"], health["status"])
    )

    card = http_request(agent["card"], timeout=timeout)
    card_data = _json(card["body"]) if card["ok"] else None
    card_ok = bool(
        card["ok"]
        and isinstance(card_data, dict)
        and card_data.get("name")
        and isinstance(card_data.get("skills"), list)
        and card_data["skills"]
    )
    if card_ok:
        detail = f"{card_data.get('name')} / {len(card_data['skills'])} skill(s) / A2A {card_data.get('protocolVersion', '?')}"
    else:
        detail = card["error"] or "agent card invalid"
    checks.append(
        _check("card", "A2A agent card", agent["card"], card_ok, detail, card["latency_ms"], card["status"])
    )

    test = http_request(agent["test_url"], timeout=timeout)
    test_ok = bool(test["ok"] and len(test["body"]) > 100)
    if test_ok:
        detail = f"{test['status']} / {test['content_type'].split(';')[0]} / {len(test['body'])} bytes"
    else:
        detail = test["error"] or f"only {len(test['body'])} bytes"
    checks.append(
        _check("public_test", "Public test artifact", agent["test_url"], test_ok, detail, test["latency_ms"], test["status"])
    )

    passed = sum(1 for c in checks if c["ok"])
    if checks[0]["ok"] and checks[2]["ok"] and passed == len(checks):
        status = "live"
    elif checks[0]["ok"] and checks[2]["ok"]:
        status = "degraded"
    else:
        status = "down"

    return {
        "id": agent["id"],
        "name": agent["name"],
        "handle": agent.get("handle", ""),
        "market_agent_id": agent.get("market_agent_id", ""),
        "category": agent.get("category", ""),
        "listing": agent["listing"],
        "price_usd": agent["price_usd"],
        "price_unit": agent.get("price_unit", ""),
        "sla": agent["sla"],
        "status": status,
        "delivered_jobs": delivered,
        "checks_passed": passed,
        "checks_total": len(checks),
        "checks": checks,
    }


def check_fleet(catalog: dict[str, Any], timeout: float = 25.0) -> dict[str, Any]:
    started = time.monotonic()
    agents = catalog["agents"]
    results: list[dict[str, Any] | None] = [None] * len(agents)
    with ThreadPoolExecutor(max_workers=min(6, len(agents))) as pool:
        futures = {
            pool.submit(check_agent, agent, timeout): index
            for index, agent in enumerate(agents)
        }
        for future, index in futures.items():
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = {
                    "id": agents[index]["id"],
                    "name": agents[index]["name"],
                    "status": "down",
                    "error": f"{type(exc).__name__}: {exc}",
                    "checks": [],
                    "checks_passed": 0,
                    "checks_total": 0,
                }
    final = [r for r in results if r is not None]
    summary = {
        "agents": len(final),
        "live": sum(1 for r in final if r.get("status") == "live"),
        "degraded": sum(1 for r in final if r.get("status") == "degraded"),
        "down": sum(1 for r in final if r.get("status") == "down"),
        "checks_passed": sum(int(r.get("checks_passed", 0)) for r in final),
        "checks_total": sum(int(r.get("checks_total", 0)) for r in final),
    }
    return {
        "fleet": catalog.get("fleet", "NEAR Agent Fleet"),
        "market": catalog.get("market", "https://market.near.ai"),
        "generated_at": utcnow(),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "summary": summary,
        "agents": final,
    }


def render_console(status: dict[str, Any]) -> str:
    """Compact, aligned terminal view for demos and humans."""
    green = "\033[38;5;154m"
    dim = "\033[2m"
    red = "\033[38;5;203m"
    amber = "\033[38;5;214m"
    reset = "\033[0m"
    s = status["summary"]
    out = [
        f"{green}NEAR AGENT FLEET{reset}  {dim}live verification · {status['generated_at']}{reset}",
        f"{s['live']}/{s['agents']} agents live · {s['checks_passed']}/{s['checks_total']} checks passed · {status['duration_ms']} ms",
        "",
        f"{'AGENT':38} {'PRICE':>7}  {'SLA':8} {'STATUS':9} {'CHECKS':6} {'JOBS':>5}",
        f"{'-'*38} {'-'*7}  {'-'*8} {'-'*9} {'-'*6} {'-'*5}",
    ]
    for a in status["agents"]:
        colour = green if a["status"] == "live" else amber if a["status"] == "degraded" else red
        name = a["name"][:38]
        out.append(
            f"{name:38} {('$'+a['price_usd']):>7}  {a['sla']:8} "
            f"{colour}{a['status'].upper():9}{reset} {a['checks_passed']}/{a['checks_total']:<4} "
            f"{(a.get('delivered_jobs') or '—'):>5}"
        )
        failed = [c["label"] for c in a.get("checks", []) if not c["ok"]]
        detail = "all public checks pass" if not failed else "failed: " + ", ".join(failed)
        out.append(f"{dim}  {detail}{reset}")
    return "\n".join(out)


def render_markdown(status: dict[str, Any]) -> str:
    s = status["summary"]
    lines = [
        f"# {status['fleet']} — live verification",
        "",
        f"**Generated:** {status['generated_at']}  ",
        f"**Result:** {s['live']}/{s['agents']} live, {s['degraded']} degraded, {s['down']} down  ",
        f"**Checks:** {s['checks_passed']}/{s['checks_total']} passed in {status['duration_ms']} ms",
        "",
        "| Agent | Price | SLA | Status | Checks | Delivered jobs |",
        "|---|---|---|---|---|---|",
    ]
    for a in status["agents"]:
        lines.append(
            f"| [{a['name']}]({a['listing']}) | ${a['price_usd']} | {a['sla']} | "
            f"**{a['status']}** | {a['checks_passed']}/{a['checks_total']} | {a.get('delivered_jobs') or '—'} |"
        )
    lines += ["", "## Checks", ""]
    for a in status["agents"]:
        lines.append(f"### {a['name']}")
        lines.append("")
        for c in a.get("checks", []):
            mark = "PASS" if c["ok"] else "FAIL"
            lines.append(f"- **{mark}** {c['label']} — {c['detail']} ({c['latency_ms']} ms)")
        lines.append("")
    return "\n".join(lines)


def route_brief(brief: str, catalog: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(str(brief or "").lower().split())
    if not text:
        return {
            "brief": brief,
            "confidence": 0.0,
            "needs_clarification": True,
            "recommendation": None,
            "alternatives": [],
            "reason": "empty brief",
        }

    scored: list[tuple[float, dict[str, Any], list[str]]] = []
    for agent in catalog["agents"]:
        matched: list[str] = []
        score = 0.0
        for keyword in agent.get("keywords", []):
            key = keyword.lower()
            if " " in key:
                hits = text.count(key)
            else:
                hits = len(re.findall(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", text))
            if hits:
                matched.append(keyword)
                score += hits * (2.0 if " " in key else 1.0)
        scored.append((score, agent, matched))

    scored.sort(key=lambda row: (-row[0], row[1]["id"]))
    top_score, top_agent, top_matched = scored[0]
    total = sum(row[0] for row in scored) or 1.0
    confidence = round(min(0.99, top_score / total), 2) if top_score else 0.0
    needs_clarification = top_score == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0])

    def quote(agent: dict[str, Any], matched: list[str], score: float) -> dict[str, Any]:
        return {
            "id": agent["id"],
            "name": agent["name"],
            "price_usd": agent["price_usd"],
            "price_unit": agent.get("price_unit", ""),
            "sla": agent["sla"],
            "listing": agent["listing"],
            "matched_keywords": matched,
            "score": score,
        }

    recommendation = None if needs_clarification else quote(top_agent, top_matched, top_score)
    alternatives = [
        quote(agent, matched, score)
        for score, agent, matched in scored[1:]
        if score > 0
    ]
    if needs_clarification:
        reason = "no specialist matched" if top_score == 0 else "two specialists matched equally"
    else:
        reason = "matched " + ", ".join(top_matched)

    return {
        "brief": brief,
        "confidence": confidence,
        "needs_clarification": needs_clarification,
        "recommendation": recommendation,
        "alternatives": alternatives,
        "reason": reason,
    }


BRAND_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .&'\u2019-]{1,39}")


def brand_check(name: str, catalog: dict[str, Any], timeout: float = 120.0) -> dict[str, Any]:
    """Run a real brand check through the live agent. Returns a JSON-safe result."""
    name = str(name or "").strip()
    if not BRAND_NAME_RE.fullmatch(name):
        return {"ok": False, "error": "name must be 2-40 letters, digits, spaces, . & ' or -"}
    brand = next((a for a in catalog["agents"] if a["id"] == "brand-checker"), None)
    if not brand:
        return {"ok": False, "error": "brand agent not configured"}
    endpoint = brand["health"].rsplit("/", 1)[0] + "/v1/check"
    payload = {
        "names": [name],
        "domains": [".studio", ".dev"],
        "platforms": ["x"],
        "countries": [],
        "variations": False,
    }
    result = http_request(endpoint, method="POST", payload=payload, timeout=timeout)
    data = _json(result["body"]) or {}
    if not result["ok"] or data.get("error"):
        return {
            "ok": False,
            "error": data.get("error") or result["error"] or "brand agent unavailable",
            "http_status": result["status"],
        }
    report = str(data.get("report_md") or "")
    summary_lines = []
    for line in report.splitlines():
        if line.startswith("**Quick read:**"):
            summary_lines.append(re.sub(r"\*\*", "", line))
        elif line.startswith("| ") and ("Available" in line or "Taken" in line) and "Domain" not in line:
            cells = [c.strip().strip("*") for c in line.strip("|").split("|")]
            if len(cells) >= 2:
                summary_lines.append(f"{cells[0]}: {cells[1]}")
        if len(summary_lines) >= 4:
            break
    return {
        "ok": True,
        "name": name,
        "job_id": data.get("job_id"),
        "report_url": data.get("report_url"),
        "result_url": data.get("result_url"),
        "summary": "\n".join(summary_lines) or "Report ready.",
        "report_md": report[:6000],
    }


def _normalise_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    return {
        "fleet": catalog.get("fleet", "NEAR Agent Fleet"),
        "market": catalog.get("market", "https://market.near.ai"),
        "verified_at": catalog.get("verified_at", ""),
        "agents": [
            {
                "id": a["id"],
                "name": a["name"],
                "handle": a.get("handle", ""),
                "category": a.get("category", ""),
                "listing": a["listing"],
                "price_usd": a["price_usd"],
                "price_unit": a.get("price_unit", ""),
                "sla": a["sla"],
                "skills": a.get("skills", []),
            }
            for a in catalog["agents"]
        ],
    }


DASHBOARD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NEAR Agent Fleet — live control plane</title>
<style>
  :root {
    --bg: #0b0b0b; --panel: #141414; --line: #2a2a2a; --ink: #f2ead8;
    --muted: #9c9484; --green: #b8ff5a; --amber: #ffc857; --red: #ff6b5e;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--ink); }
  body { font-family: "SFMono-Regular", ui-monospace, Menlo, Consolas, monospace; overflow: hidden; }
  .wrap { height: 100vh; display: grid; grid-template-rows: auto 1fr auto auto; gap: 10px; padding: 16px 20px 12px; }
  header { display: flex; align-items: baseline; justify-content: space-between; border-bottom: 1px solid var(--line); padding-bottom: 10px; }
  h1 { font-size: 22px; letter-spacing: .12em; margin: 0; font-weight: 700; }
  h1 span { color: var(--green); }
  .sub { color: var(--muted); font-size: 11px; letter-spacing: .06em; }
  .pill { display: inline-block; padding: 2px 8px; border: 1px solid var(--line); border-radius: 999px; font-size: 10px; letter-spacing: .08em; }
  .live { color: var(--green); border-color: #3f5f22; background: #17200d; }
  .degraded { color: var(--amber); border-color: #5f4a1c; background: #201a0b; }
  .down { color: var(--red); border-color: #5f2822; background: #200d0b; }
  .cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; align-content: start; }
  .card { background: var(--panel); border: 1px solid var(--line); padding: 12px 14px; min-height: 238px; display: flex; flex-direction: column; }
  .card h2 { font-size: 14px; line-height: 1.25; margin: 8px 0 4px; }
  .meta { color: var(--muted); font-size: 10px; line-height: 1.5; }
  .price { font-size: 26px; color: var(--green); margin: 6px 0 0; }
  .price small { font-size: 10px; color: var(--muted); }
  ul { margin: 8px 0 0; padding-left: 14px; color: #cfc7b7; font-size: 10px; line-height: 1.55; }
  .card a { color: var(--ink); font-size: 10px; margin-top: auto; padding-top: 8px; }
  .console { display: grid; grid-template-columns: 1.15fr .85fr; gap: 10px; min-height: 0; }
  .box { background: var(--panel); border: 1px solid var(--line); padding: 10px 12px; overflow: hidden; }
  .box h3 { margin: 0 0 8px; font-size: 10px; letter-spacing: .12em; color: var(--muted); font-weight: 600; }
  #log { font-size: 10px; line-height: 1.55; color: #cfc7b7; height: 104px; overflow: hidden; white-space: pre-wrap; }
  .cmd { color: var(--green); }
  .row { display: flex; gap: 6px; }
  input { flex: 1; background: #0b0b0b; color: var(--ink); border: 1px solid var(--line); padding: 8px 9px; font: inherit; font-size: 11px; }
  button { background: var(--green); color: #0b0b0b; border: 0; padding: 8px 12px; font: inherit; font-size: 11px; font-weight: 700; cursor: pointer; }
  button:disabled { opacity: .4; cursor: wait; }
  #routeResult, #brandResult { margin-top: 8px; font-size: 10px; line-height: 1.5; color: #cfc7b7; height: 92px; overflow: hidden; }
  #routeResult b, #brandResult b { color: var(--green); }
  footer { border-top: 1px solid var(--line); padding-top: 9px; display: flex; justify-content: space-between; gap: 20px; align-items: center; }
  #caption { font-size: 12px; color: var(--ink); }
  #caption span { color: var(--green); }
  .foot { color: var(--muted); font-size: 10px; white-space: nowrap; }
  .flash { animation: flash .6s ease; }
  @keyframes flash { 0% { background: #23320f; } 100% { background: var(--panel); } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>NEAR <span>AGENT FLEET</span></h1>
      <div class="sub">THREE PAID AGENTS · ONE OPERATOR · LIVE ON NEAR AGENT MARKET</div>
    </div>
    <div class="sub" id="clock">—</div>
  </header>

  <section class="cards" id="cards"></section>

  <section class="console">
    <div class="box">
      <h3>&gt;_ ROUTER — DESCRIBE THE JOB, GET THE SPECIALIST</h3>
      <div class="row">
        <input id="brief" value="I need five vertical clips with captions from a 40-minute podcast" spellcheck="false">
        <button id="routeBtn">Route</button>
      </div>
      <div id="routeResult">Waiting for a brief.</div>
    </div>
    <div class="box">
      <h3>&gt;_ LIVE BRAND CHECK — REAL PUBLIC JOB, NO CHARGE</h3>
      <div class="row">
        <input id="brandName" value="vellumforge" spellcheck="false" maxlength="40">
        <button id="brandBtn">Check</button>
      </div>
      <div id="brandResult">Checks .studio and .dev plus the X handle through the live agent.</div>
    </div>
  </section>

  <footer>
    <div id="caption"><span>●</span> Verifying three public listings, health endpoints and A2A cards.</div>
    <div class="foot">github.com/matrixneoaiagent/near-agent-fleet</div>
  </footer>
</div>

<script>
const DEMO = new URLSearchParams(location.search).has('demo');
const $ = (id) => document.getElementById(id);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function caption(text) { $('caption').innerHTML = '<span>●</span> ' + text; }
function logLine(text, cls = '') {
  const line = document.createElement('div');
  if (cls) line.className = cls;
  line.textContent = text;
  $('log').appendChild(line);
  $('log').scrollTop = $('log').scrollHeight;
}
function logHTML(text) {
  const line = document.createElement('div');
  line.innerHTML = text;
  $('log').appendChild(line);
  $('log').scrollTop = $('log').scrollHeight;
}
async function typeText(input, text, speed = 22) {
  input.value = '';
  for (const ch of text) { input.value += ch; await sleep(speed); }
}

function cardHTML(a) {
  const status = a.status || 'unknown';
  return `<article class="card" id="card-${esc(a.id)}">
    <div><span class="pill ${status}">${status.toUpperCase()}</span></div>
    <h2>${esc(a.name)}</h2>
    <div class="meta">${esc(a.category)} · ${a.market_agent_id ? esc(a.market_agent_id.slice(0, 8)) : ''} · ${esc(a.handle)}</div>
    <div class="price">$${esc(a.price_usd)} <small>/ ${esc(a.price_unit || 'job')}</small></div>
    <div class="meta">SLA ${esc(a.sla)} · checks ${a.checks_passed ?? '—'}/${a.checks_total ?? '—'}</div>
    <ul>${(a.skills || []).slice(0, 4).map(s => `<li>${esc(s)}</li>`).join('')}</ul>
    <a href="${esc(a.listing)}" target="_blank" rel="noopener">Open listing →</a>
  </article>`;
}

function renderFleet(agents) {
  $('cards').innerHTML = agents.map(cardHTML).join('');
  agents.forEach((a, i) => setTimeout(() => {
    const el = $('card-' + a.id); if (el) el.classList.add('flash');
  }, i * 180));
}

async function runStatus() {
  logLine('$ python3 fleet.py check', 'cmd');
  const t0 = performance.now();
  let data;
  try {
    const res = await fetch('/api/status');
    data = await res.json();
  } catch (err) {
    logLine('ERROR ' + err);
    return null;
  }
  renderFleet(data.agents);
  data.agents.forEach(a => {
    logLine(`  ${a.status.toUpperCase().padEnd(8)} ${a.name} — ${a.checks_passed}/${a.checks_total} checks, ${a.checks.map(c => c.latency_ms).reduce((x,y)=>x+y,0)} ms`);
  });
  logLine(`  ${data.summary.live}/${data.summary.agents} live in ${data.duration_ms} ms`, 'cmd');
  return data;
}

async function runRoute(brief, doType = true) {
  if (doType) await typeText($('brief'), brief);
  $('routeBtn').disabled = true;
  logLine('$ fleet route "' + brief.slice(0, 48) + '..."', 'cmd');
  try {
    const res = await fetch('/api/route', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({brief}) });
    const d = await res.json();
    if (d.needs_clarification || !d.recommendation) {
      $('routeResult').innerHTML = `<b>Needs clarification.</b> ${esc(d.reason)}`;
    } else {
      const r = d.recommendation;
      $('routeResult').innerHTML = `<b>${esc(r.name)}</b><br>$${esc(r.price_usd)} ${esc(r.price_unit)} · SLA ${esc(r.sla)}<br>` +
        `matched: ${esc(r.matched_keywords.join(', '))} · confidence ${(d.confidence * 100).toFixed(0)}%<br>` +
        `<a href="${esc(r.listing)}" target="_blank" rel="noopener">Hire this agent →</a>`;
      logLine(`  -> ${r.name} ($${r.price_usd}, ${r.sla})`);
    }
  } catch (err) {
    $('routeResult').textContent = 'Router error: ' + err;
  }
  $('routeBtn').disabled = false;
}

async function runBrand(name, doType = true) {
  if (doType) await typeText($('brandName'), name);
  $('brandBtn').disabled = true;
  logLine('$ POST /v1/check {"names":["' + name + '"]}', 'cmd');
  try {
    const res = await fetch('/api/brand-check', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name}) });
    const d = await res.json();
    if (!d.ok) {
      $('brandResult').innerHTML = `<b>Check failed:</b> ${esc(d.error || 'unknown error')}`;
    } else {
      const summary = (d.summary || '').replace(/\n/g, '<br>');
      $('brandResult').innerHTML = `<b>${esc(d.name)}</b> — ${summary}<br>` +
        `<a href="${esc(d.report_url)}" target="_blank" rel="noopener">Open the full report →</a>`;
      logLine('  -> ' + (d.summary || 'report ready').replace(/\n/g, ' / '));
    }
  } catch (err) {
    $('brandResult').textContent = 'Brand check error: ' + err;
  }
  $('brandBtn').disabled = false;
}

$('routeBtn').onclick = () => runRoute($('brief').value, false);
$('brandBtn').onclick = () => runBrand($('brandName').value, false);

async function demo() {
  caption('Three paid agents. One operator. All self-hosted, all live on NEAR Agent Market.');
  await sleep(4500);
  caption('Step 1 — verify every public listing, health endpoint and A2A card.');
  await runStatus();
  await sleep(4500);
  caption('Step 2 — the brand agent returns domains, handles, app stores and trademarks in one report.');
  await runRoute('Check the brand name Vellumforge on .studio, .dev and the X handle', true);
  await sleep(5000);
  caption('Step 3 — a video brief routes to the clipping specialist: $25, 6-hour SLA.');
  await runRoute('I need five vertical clips with captions from a 40-minute podcast', true);
  await sleep(5000);
  caption('Step 4 — run a real public brand check. No payment, no mock data.');
  await runBrand('vellumforge', true);
  await sleep(7000);
  caption('The fleet is three independent agents, one control plane, and a public hire link for each job.');
  await sleep(5500);
  caption('Open source · reproducible checks · honest limits. Built at NEAR HACKATHON.');
  await sleep(4500);
}

(async function init() {
  const tick = () => { $('clock').textContent = new Date().toISOString().replace('T',' ').slice(0,19) + ' UTC'; };
  tick(); setInterval(tick, 1000);
  if (DEMO) { await demo(); return; }
  caption('Verifying three public listings, health endpoints and A2A cards.');
  await runStatus();
})();
</script>
</body>
</html>
"""


def render_dashboard(catalog: dict[str, Any] | None = None) -> str:
    return DASHBOARD


AGENT_CARD = {
    "protocolVersion": "0.3.0",
    "name": "NEAR Agent Fleet Router",
    "description": "Routes a natural-language brief to one of three live paid agents on NEAR AI Agent Market, and returns the price, SLA and public hire link.",
    "url": "http://127.0.0.1:8780/v1/route",
    "preferredTransport": "JSONRPC",
    "version": "1.0.0",
    "provider": {"organization": "matrixneoaiagent"},
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["text/plain", "application/json"],
    "defaultOutputModes": ["application/json", "text/markdown"],
    "skills": [
        {
            "id": "route-to-specialist",
            "name": "Route a brief to a paid specialist",
            "description": "Scores a brief against the skills of a brand checker, a media generator and a video clipper, then quotes the best match.",
            "tags": ["agent-commerce", "routing", "quotes"],
            "examples": ["I need five vertical clips with captions from a podcast"],
        }
    ],
}


class FleetHandler(BaseHTTPRequestHandler):
    server_version = "near-agent-fleet/1.0"
    catalog: dict[str, Any] = {}
    _brand_lock = threading.Lock()
    _brand_last = 0.0

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, indent=1).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 64_000:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self._send(200, render_dashboard(self.catalog).encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/healthz":
            self._json(200, {"ok": True, "service": "near-agent-fleet", "agents": len(self.catalog.get("agents", []))})
        elif path == "/.well-known/agent-card.json":
            self._json(200, AGENT_CARD)
        elif path == "/v1/agents":
            self._json(200, _normalise_catalog(self.catalog))
        elif path == "/api/catalog":
            self._json(200, _normalise_catalog(self.catalog))
        elif path == "/api/status":
            self._json(200, check_fleet(self.catalog))
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/api/route", "/v1/route"):
            body = self._read_json()
            brief = str(body.get("brief") or body.get("query") or "").strip()
            if not brief:
                self._json(400, {"error": "brief is required"})
                return
            self._json(200, route_brief(brief, self.catalog))
            return
        if path == "/api/brand-check":
            self._brand_check()
            return
        self._json(404, {"error": "not found"})

    def _brand_check(self) -> None:
        body = self._read_json()
        now = time.monotonic()
        with FleetHandler._brand_lock:
            if now - FleetHandler._brand_last < 5:
                self._json(429, {"ok": False, "error": "one brand check every 5 seconds"})
                return
            FleetHandler._brand_last = now
        result = brand_check(str(body.get("name") or ""), self.catalog)
        if not result.get("ok"):
            self._json(400 if result.get("error", "").startswith("name must") else 502, result)
            return
        self._json(200, result)


def serve(host: str, port: int) -> None:
    catalog = load_catalog()
    FleetHandler.catalog = catalog
    httpd = ThreadingHTTPServer((host, port), FleetHandler)
    print(f"NEAR Agent Fleet on http://{host}:{port}  (demo: /?demo=1)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify, route to, and demo the NEAR Agent Fleet.")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="verify every agent live")
    check.add_argument("--json", dest="json_path", default="")
    check.add_argument("--md", dest="md_path", default="")
    check.add_argument("--html", dest="html_path", default="")
    check.add_argument("--offline", default="", help="read a saved status JSON instead of the network")
    check.add_argument("--timeout", type=float, default=25.0)

    status_cmd = sub.add_parser("status", help="compact live status table for a terminal")
    status_cmd.add_argument("--offline", default="")

    route = sub.add_parser("route", help="route a brief to one agent")
    route.add_argument("brief")
    route.add_argument("--json", action="store_true")

    brand = sub.add_parser("brand", help="run a real brand check through the live agent")
    brand.add_argument("name")
    brand.add_argument("--json", action="store_true")

    server = sub.add_parser("serve", help="run the live dashboard and router API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8780)

    sub.add_parser("catalog", help="print the agent catalog")

    args = parser.parse_args(argv)
    catalog = load_catalog()

    if args.command == "catalog":
        for agent in catalog["agents"]:
            print(f"{agent['id']:16} ${agent['price_usd']:>6}  {agent['sla']:8}  {agent['listing']}")
        return 0

    if args.command == "route":
        result = route_brief(args.brief, catalog)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            rec = result["recommendation"]
            if rec:
                print(f"{rec['name']} — ${rec['price_usd']} {rec['price_unit']}, SLA {rec['sla']}")
                print(f"matched: {', '.join(rec['matched_keywords'])} (confidence {result['confidence']:.0%})")
                print(f"hire: {rec['listing']}")
            else:
                print(f"Needs clarification: {result['reason']}")
        return 0

    if args.command == "brand":
        result = brand_check(args.name, catalog)
        if args.json:
            print(json.dumps(result, indent=2))
        elif result.get("ok"):
            print(f"Brand check: {result['name']}")
            for line in result["summary"].splitlines():
                print(textwrap.fill(line, 88))
            print()
            print(textwrap.fill(f"report: {result['report_url']}", 88))
        else:
            print(f"Brand check failed: {result.get('error')}")
            return 1
        return 0

    if args.command == "serve":
        serve(args.host, args.port)
        return 0

    if args.command == "status":
        if args.offline:
            with open(args.offline, "r", encoding="utf-8") as fh:
                status = json.load(fh)
        else:
            status = check_fleet(catalog, timeout=25.0)
        print(render_console(status))
        return 0 if status["summary"]["down"] == 0 else 2

    if args.command == "check":
        if args.offline:
            with open(args.offline, "r", encoding="utf-8") as fh:
                status = json.load(fh)
        else:
            status = check_fleet(catalog, timeout=args.timeout)
        markdown = render_markdown(status)
        print(markdown)
        if args.json_path:
            Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_path).write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
            print(f"\nwrote {args.json_path}")
        if args.md_path:
            Path(args.md_path).parent.mkdir(parents=True, exist_ok=True)
            Path(args.md_path).write_text(markdown + "\n", encoding="utf-8")
            print(f"wrote {args.md_path}")
        if args.html_path:
            Path(args.html_path).parent.mkdir(parents=True, exist_ok=True)
            Path(args.html_path).write_text(render_dashboard(catalog), encoding="utf-8")
            print(f"wrote {args.html_path}")
        return 0 if status["summary"]["down"] == 0 else 2

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
