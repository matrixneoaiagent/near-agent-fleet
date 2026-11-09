"""Real availability checks for the Brand Name Availability Checker.

Every result is an observation with a timestamp and a source.  Nothing here
registers, reserves or buys a name.  When a platform blocks an automated
lookup the result is "unknown" with the reason, never a guess.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

UA = "BrandNameAvailabilityChecker/1.0 (+https://market.near.ai/a/brandnamechecker)"
DEFAULT_DOMAINS = [".com", ".io", ".ai", ".co"]
DEFAULT_PLATFORMS = ["x", "github", "youtube", "tiktok", "instagram"]
DEFAULT_COUNTRIES = ["US"]
PLATFORM_LABELS = {
    "x": "X (Twitter)",
    "github": "GitHub",
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "reddit": "Reddit",
    "facebook": "Facebook",
    "threads": "Threads",
}
COUNTRY_OFFICES = {
    "US": "US", "EU": "EM", "GB": "GB", "UK": "GB", "DE": "DE", "FR": "FR",
    "IT": "IT", "ES": "ES", "NL": "NL", "PL": "PL", "RO": "RO", "CA": "CA",
    "AU": "AU", "JP": "JP", "CN": "CN", "BR": "BR", "IN": "IN", "CH": "CH",
    "SE": "SE", "NO": "NO", "DK": "DK", "FI": "FI", "IE": "IE", "AT": "AT",
    "BE": "BE", "PT": "PT", "CZ": "CZ", "GR": "GR", "TR": "TR", "RU": "RU",
    "UA": "UA", "MX": "MX", "KR": "KR", "SG": "SG", "NZ": "NZ", "ZA": "ZA",
    "AE": "AE", "IL": "IL", "AR": "AR", "CL": "CL", "CO": "CO",
}

_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 300.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def cache_get(key: str) -> Any:
    with _CACHE_LOCK:
        row = _CACHE.get(key)
        if row and time.time() - row[0] < _CACHE_TTL:
            return row[1]
    return None


def cache_set(key: str, value: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), value)
        if len(_CACHE) > 5000:
            for old in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:1000]:
                _CACHE.pop(old, None)


def http(
    url: str,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 18.0,
) -> tuple[int, dict[str, str], bytes]:
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except Exception as exc:  # SSL reset, timeout, DNS
        return 0, {}, str(exc).encode()


def _result(status: str, detail: str, checked_at: str, source: str, **extra: Any) -> dict[str, Any]:
    row = {
        "status": status,
        "detail": detail,
        "checked_at": checked_at,
        "source": source,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# Domains: RDAP first, registry WHOIS as confirmation/fallback


RDAP_BASES = {
    "com": "https://rdap.verisign.com/com/v1/domain/",
    "net": "https://rdap.verisign.com/net/v1/domain/",
    "io": "https://rdap.identitydigital.services/rdap/domain/",
    "ai": "https://rdap.identitydigital.services/rdap/domain/",
    "org": "https://rdap.org/domain/",
    "app": "https://rdap.org/domain/",
    "dev": "https://rdap.org/domain/",
    "xyz": "https://rdap.org/domain/",
}
WHOIS_SERVERS = {
    "com": "whois.verisign-grs.com",
    "net": "whois.verisign-grs.com",
    "org": "whois.publicinterestregistry.org",
    "io": "whois.nic.io",
    "ai": "whois.nic.ai",
    "co": "whois.registry.co",
    "xyz": "whois.nic.xyz",
    "us": "whois.nic.us",
    "de": "whois.denic.de",
    "fr": "whois.nic.fr",
    "eu": "whois.eu",
}


def _whois(server: str, query: str, timeout: float = 12.0) -> str:
    sock = socket.create_connection((server, 43), timeout=timeout)
    try:
        sock.sendall((query + "\r\n").encode())
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")
    finally:
        sock.close()


def _iana_whois(tld: str) -> str | None:
    key = f"iana:{tld}"
    cached = cache_get(key)
    if cached is not None:
        return cached or None
    status, _, body = http(f"https://whois.iana.org/{urllib.parse.quote(tld)}", timeout=10)
    server = None
    if status == 200:
        match = re.search(r"(?im)^whois:\s*(\S+)", body.decode("utf-8", "replace"))
        if match:
            server = match.group(1).strip()
    cache_set(key, server or "")
    return server


def _rdap_taken(data: dict[str, Any]) -> dict[str, Any]:
    registrar = None
    for entity in data.get("entities") or []:
        roles = [str(r).lower() for r in (entity.get("roles") or [])]
        if "registrar" in roles:
            vcard = entity.get("vcardArray") or []
            if len(vcard) > 1 and isinstance(vcard[1], list):
                for item in vcard[1]:
                    if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
                        registrar = item[3]
                        break
    events = {}
    for event in data.get("events") or []:
        action = event.get("eventAction")
        if action:
            events[action] = event.get("eventDate")
    return {
        "registrar": registrar,
        "created": events.get("registration"),
        "expires": events.get("expiration"),
        "registry_status": data.get("status") or [],
    }


def check_domain(domain: str) -> dict[str, Any]:
    checked = utcnow()
    domain = domain.strip().lower().rstrip(".")
    key = f"domain:{domain}"
    cached = cache_get(key)
    if cached:
        cached = dict(cached)
        cached["checked_at"] = checked
        return cached
    tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
    rdap_base = RDAP_BASES.get(tld)
    rdap_url = (rdap_base or "https://rdap.org/domain/") + urllib.parse.quote(domain)
    status, _, body = http(rdap_url, headers={"Accept": "application/rdap+json"})
    text = body.decode("utf-8", "replace")
    if status == 200:
        try:
            data = json.loads(text)
        except Exception:
            data = {}
        info = _rdap_taken(data)
        detail = "Registered"
        if info.get("registrar"):
            detail += f" — registrar: {info['registrar']}"
        if info.get("expires"):
            detail += f", expires {str(info['expires'])[:10]}"
        row = _result("taken", detail, checked, rdap_url, **info)
        cache_set(key, row)
        return row
    # A 404 can mean "free" or "this registry has no RDAP service".  A WHOIS
    # lookup settles it where a registry WHOIS server exists.
    whois_server = WHOIS_SERVERS.get(tld)
    if not whois_server:
        whois_server = _iana_whois(tld) if tld else None
    whois_text = ""
    whois_error = ""
    if whois_server:
        try:
            whois_text = _whois(whois_server, domain)
        except Exception as exc:
            whois_error = str(exc)
    low = whois_text.lower()
    free_patterns = (
        "no match for", "domain not found", "not found", "no data found",
        "no entries found", "the queried object does not exist",
        "status: free", "available for registration", "no object found",
    )
    lookup = f"https://lookup.icann.org/en/lookup?name={urllib.parse.quote(domain)}"
    if whois_text and any(p in low for p in free_patterns):
        detail = "No registry record at check time"
        if whois_server:
            detail += f" (WHOIS {whois_server})"
        row = _result("available", detail, checked, lookup, whois_server=whois_server, rdap_source=rdap_url)
        cache_set(key, row)
        return row
    if whois_text and re.search(r"(?im)^\s*(domain name|registry domain id|creation date)\s*:", whois_text):
        registrar = None
        expires = None
        m = re.search(r"(?im)^\s*registrar\s*:\s*(.+)$", whois_text)
        if m:
            registrar = m.group(1).strip()
        m = re.search(r"(?im)^\s*registry expiry date\s*:\s*(.+)$", whois_text)
        if m:
            expires = m.group(1).strip()
        detail = "Registered"
        if registrar:
            detail += f" — registrar: {registrar}"
        if expires:
            detail += f", expires {expires[:10]}"
        row = _result("taken", detail, checked, lookup, registrar=registrar, expires=expires,
                      whois_server=whois_server, rdap_source=rdap_url)
        cache_set(key, row)
        return row
    if status == 404 and not whois_server:
        # Registries such as .app/.dev publish RDAP only.
        row = _result("available", "No RDAP record at check time", checked, rdap_url)
        cache_set(key, row)
        return row
    reason = f"RDAP HTTP {status or 'network error'}"
    if whois_error:
        reason += f"; WHOIS error: {whois_error[:120]}"
    elif whois_text:
        reason += "; WHOIS response was not conclusive"
    row = _result("unknown", reason, checked, rdap_url)
    cache_set(key, row)
    return row


# ---------------------------------------------------------------------------
# Social handles with public, observable signals


def check_x(handle: str) -> dict[str, Any]:
    checked = utcnow()
    url = f"https://x.com/{urllib.parse.quote(handle)}"
    status, _, body = http(url)
    text = body.decode("utf-8", "replace")
    if status == 404 or "user profile not found" in text.lower():
        return _result("available", "X returned the public 'profile not found' page", checked, url)
    if status == 200:
        if "account suspended" in text.lower():
            return _result("taken", "Handle exists but the account is suspended", checked, url)
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        return _result("taken", f"Public profile exists ({html.unescape(title.group(1)).strip()[:90] if title else 'HTTP 200'})", checked, url)
    return _result("unknown", f"X returned HTTP {status or 'network error'}; verify manually", checked, url)


def check_github(handle: str) -> dict[str, Any]:
    checked = utcnow()
    url = f"https://api.github.com/users/{urllib.parse.quote(handle)}"
    status, _, body = http(url, headers={"Accept": "application/vnd.github+json"})
    if status == 200:
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            data = {}
        return _result("taken", f"GitHub user @{data.get('login') or handle} exists", checked, url,
                       profile=data.get("html_url"))
    if status == 404:
        return _result("available", "GitHub API returned 404 for this login", checked, url)
    return _result("unknown", f"GitHub API returned HTTP {status or 'network error'}", checked, url)


def check_youtube(handle: str) -> dict[str, Any]:
    checked = utcnow()
    url = f"https://www.youtube.com/@{urllib.parse.quote(handle)}"
    status, _, body = http(url)
    text = body.decode("utf-8", "replace")
    if status == 404:
        return _result("available", "YouTube returned 404 for this @handle", checked, url)
    if status == 200:
        title = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        return _result("taken", f"Channel page exists ({html.unescape(title.group(1)).strip()[:90] if title else 'HTTP 200'})", checked, url)
    return _result("unknown", f"YouTube returned HTTP {status or 'network error'}", checked, url)


def check_tiktok(handle: str) -> dict[str, Any]:
    checked = utcnow()
    url = f"https://www.tiktok.com/@{urllib.parse.quote(handle)}"
    oembed = "https://www.tiktok.com/oembed?url=" + urllib.parse.quote(url, safe="")
    status, _, body = http(oembed)
    if status == 200:
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            data = {}
        return _result("taken", f"TikTok profile exists ({data.get('author_name') or handle})", checked, oembed,
                       profile=url)
    if status in (400, 404):
        return _result("available", f"TikTok oEmbed returned HTTP {status} (no profile)", checked, oembed)
    return _result("unknown", f"TikTok oEmbed returned HTTP {status or 'network error'}", checked, oembed)


def check_instagram(handle: str) -> dict[str, Any]:
    checked = utcnow()
    url = f"https://www.instagram.com/{urllib.parse.quote(handle)}/"
    status, _, body = http(url)
    text = body.decode("utf-8", "replace").lower()
    if status == 404 or "page not found" in text[:4000]:
        return _result("available", "Instagram returned a public 'page not found' response", checked, url)
    return _result(
        "unknown",
        "Instagram only serves a logged-out error/login wall to automated checks; confirm manually",
        checked,
        url,
    )


def check_blocked(handle: str, platform: str) -> dict[str, Any]:
    checked = utcnow()
    urls = {
        "reddit": f"https://www.reddit.com/user/{urllib.parse.quote(handle)}/",
        "facebook": f"https://www.facebook.com/{urllib.parse.quote(handle)}",
        "threads": f"https://www.threads.net/@{urllib.parse.quote(handle)}",
    }
    return _result(
        "unknown",
        f"{PLATFORM_LABELS.get(platform, platform)} blocks or hides automated handle lookups; confirm manually",
        checked,
        urls.get(platform, ""),
    )


def check_platform(platform: str, handle: str) -> dict[str, Any]:
    key = f"platform:{platform}:{handle.lower()}"
    cached = cache_get(key)
    if cached:
        row = dict(cached)
        row["checked_at"] = utcnow()
        return row
    funcs = {
        "x": check_x,
        "github": check_github,
        "youtube": check_youtube,
        "tiktok": check_tiktok,
        "instagram": check_instagram,
    }
    if platform in funcs:
        row = funcs[platform](handle)
    else:
        row = check_blocked(handle, platform)
    cache_set(key, row)
    return row


# ---------------------------------------------------------------------------
# App stores


def check_itunes(name: str, country: str = "us") -> dict[str, Any]:
    checked = utcnow()
    url = (
        "https://itunes.apple.com/search?term="
        + urllib.parse.quote(name)
        + f"&entity=software&country={urllib.parse.quote(country)}&limit=200"
    )
    status, _, body = http(url)
    if status != 200:
        return _result("unknown", f"iTunes Search API returned HTTP {status or 'network error'}", checked, url,
                       store="Apple App Store")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return _result("unknown", "iTunes Search API returned invalid JSON", checked, url,
                       store="Apple App Store")
    matches = []
    for item in data.get("results") or []:
        if norm(item.get("trackName")) == norm(name):
            matches.append({
                "title": item.get("trackName"),
                "developer": item.get("sellerName") or item.get("artistName"),
                "bundle_id": item.get("bundleId"),
                "url": item.get("trackViewUrl"),
            })
    if matches:
        return _result("found", f"{len(matches)} exact-name app(s)", checked, url, apps=matches,
                       store="Apple App Store")
    return _result("none", f"Checked {data.get('resultCount', 0)} search result(s); no exact-name app",
                   checked, url, apps=[], store="Apple App Store")


def check_google_play(name: str) -> dict[str, Any]:
    checked = utcnow()
    url = "https://play.google.com/store/search?q=" + urllib.parse.quote(name) + "&c=apps&hl=en_US&gl=US"
    status, _, body = http(url)
    if status != 200:
        return _result("unknown", f"Google Play returned HTTP {status or 'network error'}", checked, url,
                       store="Google Play")
    text = body.decode("utf-8", "replace")
    titles = [html.unescape(t).strip() for t in re.findall(r'<span class="DdYX5">(.*?)</span>', text)]
    matches = []
    seen = set()
    for title in titles:
        if norm(title) == norm(name) and title not in seen:
            seen.add(title)
            matches.append({"title": title})
    if matches:
        return _result("found", f"{len(matches)} exact-name app(s)", checked, url, apps=matches,
                       store="Google Play")
    return _result("none", f"Checked {len(titles)} search result(s); no exact-name app", checked, url,
                   apps=[], store="Google Play")


def check_apps(name: str, country: str = "us") -> list[dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(check_itunes, name, country)
        f2 = pool.submit(check_google_play, name)
        return [f1.result(), f2.result()]


# ---------------------------------------------------------------------------
# Trademarks: USPTO (US federal) and TMview (other offices)


def _tm_mark(row: dict[str, Any], database: str, ratio: float) -> dict[str, Any]:
    return {
        "mark": row.get("mark"),
        "registration_number": row.get("registration_number"),
        "serial_number": row.get("serial_number"),
        "status": row.get("status"),
        "owner": row.get("owner"),
        "classes": row.get("classes") or [],
        "filed": row.get("filed"),
        "registered": row.get("registered"),
        "similarity": round(ratio, 2),
        "database": database,
        "url": row.get("url"),
    }


def uspto_search(name: str) -> dict[str, Any]:
    checked = utcnow()
    endpoint = "https://tmsearch.uspto.gov/prod-v1-0-0/tmsearch"
    esc = re.sub(r'(["\\])', r"\\\1", name.strip())
    body = {
        "query": {
            "bool": {
                "should": [
                    {"wildcard": {"WM": {"value": f"*{esc.lower()}*"}}},
                    {"match": {"WM": {"query": name}}},
                ],
                "minimum_should_match": 1,
                "filter": [{"term": {"LD": {"value": "true"}}}],
            }
        },
        "size": 50,
    }
    status, _, payload = http(endpoint, method="POST", body=body, headers={"Content-Type": "application/json"})
    if status != 200:
        return {
            "database": "USPTO Trademark Search (US federal register)",
            "status": "unknown",
            "checked_at": checked,
            "query_url": "https://tmsearch.uspto.gov/",
            "error": f"HTTP {status or 'network error'}",
            "marks": [],
        }
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except Exception:
        data = {}
    hits = ((data.get("hits") or {}).get("hits") or [])
    marks = []
    seen = set()
    for hit in hits:
        src = hit.get("source") or {}
        mark_name = src.get("wordmark") or ""
        ratio = similarity(mark_name, name)
        if ratio < 0.72:
            continue
        ident = str(src.get("id") or src.get("registrationId") or mark_name)
        if ident in seen:
            continue
        seen.add(ident)
        marks.append(_tm_mark({
            "mark": mark_name,
            "registration_number": src.get("registrationId"),
            "serial_number": src.get("id"),
            "status": src.get("statusDescription"),
            "owner": (src.get("ownerName") or [None])[0],
            "classes": src.get("internationalClass") or [],
            "filed": src.get("filedDate"),
            "registered": src.get("registrationDate"),
            "url": (
                "https://tsdr.uspto.gov/#caseNumber="
                + urllib.parse.quote(str(src.get("id") or src.get("registrationId") or ""))
                + "&caseSearchType=US_APPLICATION&caseType=DEFAULT&searchType=statusSearch"
            ),
        }, "USPTO", ratio))
    marks.sort(key=lambda m: (-m["similarity"], str(m.get("mark"))))
    return {
        "database": "USPTO Trademark Search (US federal register)",
        "status": "ok",
        "checked_at": checked,
        "total_returned": (data.get("hits") or {}).get("totalValue"),
        "query_url": "https://tmsearch.uspto.gov/",
        "marks": marks[:12],
    }


def tmview_search(name: str, countries: list[str]) -> dict[str, Any]:
    checked = utcnow()
    endpoint = "https://www.tmdn.org/tmview/api/search/results"
    offices = {COUNTRY_OFFICES.get(c.upper(), c.upper()) for c in countries}
    body = {
        "page": 1,
        "pageSize": 100,
        "criteria": "C",
        "basicSearch": name,
        "fOffices": sorted(offices),
    }
    status, _, payload = http(
        endpoint,
        method="POST",
        body=body,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.tmdn.org",
            "Referer": "https://www.tmdn.org/tmview/",
        },
        timeout=25,
    )
    if payload[:1] == b"<":
        return {
            "database": "TMview (EUIPO / TMDN federated register)",
            "status": "unknown",
            "checked_at": checked,
            "query_url": "https://www.tmdn.org/tmview/",
            "error": "TMview rejected the automated request (WAF); check manually",
            "marks": [],
        }
    if status != 200:
        return {
            "database": "TMview (EUIPO / TMDN federated register)",
            "status": "unknown",
            "checked_at": checked,
            "query_url": "https://www.tmdn.org/tmview/",
            "error": f"HTTP {status or 'network error'}",
            "marks": [],
        }
    try:
        data = json.loads(payload.decode("utf-8", "replace"))
    except Exception:
        data = {}
    rows = data.get("tradeMarks") or []
    marks = []
    seen = set()
    for row in rows:
        if offices and str(row.get("tmOffice") or "").upper() not in offices:
            continue
        ratio = similarity(row.get("tmName"), name)
        if ratio < 0.72:
            continue
        ident = str(row.get("ST13") or row.get("registrationNumber") or row.get("tmName"))
        if ident in seen:
            continue
        seen.add(ident)
        marks.append(_tm_mark({
            "mark": row.get("tmName"),
            "registration_number": row.get("registrationNumber"),
            "serial_number": row.get("applicationNumber"),
            "status": row.get("tradeMarkStatus"),
            "owner": (row.get("applicantName") or [None])[0],
            "classes": row.get("niceClass") or [],
            "filed": row.get("applicationDate"),
            "registered": row.get("registrationDate"),
            "url": row.get("tmOfficeURL"),
        }, f"TMview/{row.get('tmOffice')}", ratio))
    marks.sort(key=lambda m: (-m["similarity"], str(m.get("mark"))))
    return {
        "database": "TMview (EUIPO / TMDN federated register)",
        "status": "ok",
        "checked_at": checked,
        "offices": sorted(offices),
        "query_url": "https://www.tmdn.org/tmview/",
        "marks": marks[:12],
    }


def check_trademarks(name: str, countries: list[str]) -> dict[str, Any]:
    countries = [str(c).upper() for c in (countries or DEFAULT_COUNTRIES)]
    databases = []
    marks = []
    if "US" in countries:
        us = uspto_search(name)
        databases.append(us)
        marks.extend(us.get("marks") or [])
    other = [c for c in countries if c != "US"]
    if other:
        tv = tmview_search(name, other)
        databases.append(tv)
        marks.extend(tv.get("marks") or [])
    # de-duplicate across databases by mark + registration/serial
    unique = []
    seen = set()
    for mark in marks:
        key = (norm(mark.get("mark")), str(mark.get("registration_number") or mark.get("serial_number")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(mark)
    unique.sort(key=lambda m: (-m.get("similarity", 0), str(m.get("mark"))))
    return {
        "countries": countries,
        "databases": databases,
        "marks": unique[:20],
        "disclaimer": "Trademark results are a starting point, not legal advice. Similarity is a text comparison, not a legal judgment.",
    }


# ---------------------------------------------------------------------------
# Request parsing


def _split_list(text: str) -> list[str]:
    return [x.strip(" \t\"'`") for x in re.split(r"[,;/\n]+", text) if x.strip(" \t\"'`")]


NAME_STOPWORDS = {
    "a", "an", "the", "and", "or", "with", "for", "from", "on", "in", "at",
    "to", "of", "by", "as", "is", "are", "be", "please", "check", "checks",
    "checking", "test", "search", "evaluate", "look", "up", "name", "names",
    "brand", "brands", "candidate", "candidates", "report", "research",
    "domain", "domains", "extension", "extensions", "handle", "handles",
    "platform", "platforms", "trademark", "trademarks", "app", "apps",
    "store", "stores", "social", "media", "variation", "variations",
    "suggest", "suggested", "alternative", "alternatives", "whether",
    "free", "available", "taken", "across", "each", "all", "major",
    "instagram", "tiktok", "youtube", "github", "twitter", "reddit",
    "facebook", "threads", "x", "us", "usa", "eu", "uk", "gb",
}


def _clean_candidate(value: str) -> str:
    value = value.strip(" \t\r\n\"'`*#-:;,.")
    return re.sub(r"\s+", " ", value)


def _looks_like_name(value: str) -> bool:
    value = _clean_candidate(value)
    if not (2 <= len(value) <= 40):
        return False
    if "." in value:
        return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 '&._-]*", value):
        return False
    words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in value.split()]
    if not words or any(w in NAME_STOPWORDS for w in words):
        return False
    if len(words) > 3:
        return False
    if re.search(r"(?i)\b(?:and|or)\b", value):
        return False
    return True


def _extract_names(raw: str, allow_line_scan: bool = True) -> list[str]:
    candidates: list[str] = []
    # Quoted or backticked names are the strongest signal.
    for match in re.finditer(r"[\"'`]([^\"'`\n]{2,40})[\"'`]", raw):
        value = _clean_candidate(match.group(1))
        if _looks_like_name(value):
            candidates.append(value)
    # Explicit labels and "check the names ..." phrasing.
    patterns = [
        r"(?im)^\s*(?:names?|brands?|candidates?)\s*(?:are|:|-)\s*(.+)$",
        r"(?i)\b(?:check|test|search|evaluate|look up)\s+(?:the\s+)?(?:names?|brands?|candidates?)\s*(?:are|:|-)?\s*([^\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw)
        if not match:
            continue
        clause = re.split(r"\s+(?:on|for|across|in|with)\s+", match.group(1), maxsplit=1)[0]
        for part in _split_list(clause):
            value = _clean_candidate(part)
            if _looks_like_name(value):
                candidates.append(value)
        if candidates:
            break
    if not candidates:
        match = re.search(
            r"(?i)\b(?:check|test|search|evaluate|look up)\s+(?:the\s+)?(?:name|brand|candidate)?\s*"
            r"([A-Za-z0-9][\w .'&-]{1,39}?)(?=\s+(?:on|for|across|in|with)\b|[,;\n]|$)",
            raw,
        )
        if match:
            value = _clean_candidate(match.group(1))
            if _looks_like_name(value):
                candidates.append(value)
    if not candidates and allow_line_scan:
        for line in raw.splitlines():
            low = line.strip().lower()
            if low.startswith(("an agent that checks", "this agent", "what we need")):
                continue
            if len(line.split()) > 8:
                continue
            for token in _split_list(line):
                value = _clean_candidate(token)
                if _looks_like_name(value):
                    candidates.append(value)
            if candidates:
                break
    return list(dict.fromkeys(candidates))[:5]


def parse_request(text: str, allow_line_scan: bool = True) -> dict[str, Any]:
    """Turn a buyer brief or JSON into names, extensions, platforms, countries."""
    raw = (text or "").strip()
    request: dict[str, Any] = {}
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                request = obj
        except Exception:
            request = {}
    if not request:
        lower = raw.lower()
        request["names"] = _extract_names(raw, allow_line_scan=allow_line_scan)
        request["domains"] = list(dict.fromkeys(
            re.findall(r"\.(?:com|io|ai|co|net|org|app|dev|xyz|us|de|fr|eu|uk)\b", lower)
        ))
        platforms = []
        for key in PLATFORM_LABELS:
            if key == "x":
                if re.search(r"\bX\b", raw) or re.search(r"(?i)twitter", raw):
                    platforms.append(key)
            elif re.search(rf"(?i)\b{re.escape(key)}\b", raw):
                platforms.append(key)
        request["platforms"] = platforms
        countries = []
        country_terms = {
            "US": r"\bUS\b|\bUSA\b|\bU\.S\.\b|\bUnited States\b|(?i:\bus trademarks?\b)",
            "EU": r"\bEU\b|\bEUIPO\b|\bEuropean Union\b",
            "GB": r"\bGB\b|\bUK\b|\bUnited Kingdom\b",
            "DE": r"\bDE\b|\bGermany\b",
            "FR": r"\bFR\b|\bFrance\b",
            "IT": r"\bIT\b|\bItaly\b",
            "ES": r"\bES\b|\bSpain\b",
            "NL": r"\bNL\b|\bNetherlands\b",
            "PL": r"\bPL\b|\bPoland\b",
            "RO": r"\bRO\b|\bRomania\b",
            "CA": r"\bCA\b|\bCanada\b",
            "AU": r"\bAU\b|\bAustralia\b",
            "JP": r"\bJP\b|\bJapan\b",
            "CN": r"\bCN\b|\bChina\b",
            "BR": r"\bBR\b|\bBrazil\b",
            "IN": r"\bIN\b|\bIndia\b",
        }
        for code, pattern in country_terms.items():
            if re.search(pattern, raw):
                countries.append(code)
        request["countries"] = list(dict.fromkeys(countries))
        request["variations"] = bool(re.search(r"(?i)variation|suggest|alternative", raw))
    # normalise
    if isinstance(request.get("names"), str):
        request["names"] = _split_list(request["names"])
    if isinstance(request.get("name"), str) and not request.get("names"):
        request["names"] = [request["name"]]
    request["names"] = [str(n).strip() for n in (request.get("names") or []) if str(n).strip()][:5]
    domains = request.get("domains") or DEFAULT_DOMAINS
    if isinstance(domains, str):
        domains = _split_list(domains)
    normalised_domains = []
    for d in domains:
        d = str(d).strip().lower()
        if not d:
            continue
        if not d.startswith("."):
            d = "." + d
        if re.fullmatch(r"\.[a-z]{2,12}", d):
            normalised_domains.append(d)
    request["domains"] = list(dict.fromkeys(normalised_domains or DEFAULT_DOMAINS))
    platforms = request.get("platforms") or DEFAULT_PLATFORMS
    if isinstance(platforms, str):
        platforms = _split_list(platforms)
    normalised_platforms = []
    for p in platforms:
        p = str(p).strip().lower()
        p = {"twitter": "x", "x (twitter)": "x", "yt": "youtube"}.get(p, p)
        if p in PLATFORM_LABELS:
            normalised_platforms.append(p)
    request["platforms"] = list(dict.fromkeys(normalised_platforms or DEFAULT_PLATFORMS))
    countries = request.get("countries") or DEFAULT_COUNTRIES
    if isinstance(countries, str):
        countries = _split_list(countries)
    request["countries"] = list(dict.fromkeys(str(c).upper().replace("UK", "GB") for c in countries)) or ["US"]
    request["variations"] = bool(request.get("variations"))
    return request


def _variations(name: str) -> list[str]:
    base = re.sub(r"[^A-Za-z0-9]", "", name).lower()
    candidates = [f"get{base}", f"{base}hq", f"{base}app", f"try{base}", f"{base}now"]
    out = []
    for cand in candidates:
        if 2 <= len(cand) <= 30 and cand not in out:
            out.append(cand)
    return out[:3]


def check_name(name: str, domains: list[str], platforms: list[str], countries: list[str]) -> dict[str, Any]:
    handle = re.sub(r"[^A-Za-z0-9._-]", "", name).lower().replace(" ", "")
    domain_names = [name.lower().replace(" ", "") + ext for ext in domains]
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        domain_futs = {d: pool.submit(check_domain, d) for d in domain_names}
        platform_futs = {p: pool.submit(check_platform, p, handle) for p in platforms}
        app_fut = pool.submit(check_apps, name, "us")
        tm_fut = pool.submit(check_trademarks, name, countries)
        domain_rows = []
        for domain, fut in domain_futs.items():
            row = fut.result()
            row["domain"] = domain
            domain_rows.append(row)
        platform_rows = []
        for platform, fut in platform_futs.items():
            row = fut.result()
            row["platform"] = platform
            row["label"] = PLATFORM_LABELS.get(platform, platform)
            row["handle"] = handle if platform != "github" else handle
            platform_rows.append(row)
        app_rows = app_fut.result()
        trademarks = tm_fut.result()
    domains_free = [r["domain"] for r in domain_rows if r["status"] == "available"]
    handles_free = [r["label"] for r in platform_rows if r["status"] == "available"]
    return {
        "name": name,
        "handle_checked": handle,
        "domains": domain_rows,
        "platforms": platform_rows,
        "apps": app_rows,
        "trademarks": trademarks,
        "summary": {
            "domains_available": domains_free,
            "domains_taken": [r["domain"] for r in domain_rows if r["status"] == "taken"],
            "handles_available": handles_free,
            "handles_taken": [r["label"] for r in platform_rows if r["status"] == "taken"],
            "handles_unconfirmed": [r["label"] for r in platform_rows if r["status"] == "unknown"],
            "apps_exact": sum(len(a.get("apps") or []) for a in app_rows),
            "trademark_conflicts": len(trademarks.get("marks") or []),
        },
    }


def check_variations(name: str, domains: list[str], platforms: list[str]) -> list[dict[str, Any]]:
    results = []
    checkable = [p for p in platforms if p != "instagram"]
    for candidate in _variations(name):
        handle = re.sub(r"[^A-Za-z0-9._-]", "", candidate).lower()
        domain_names = [candidate + ext for ext in domains]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            dfuts = {d: pool.submit(check_domain, d) for d in domain_names}
            pfuts = {p: pool.submit(check_platform, p, handle) for p in checkable}
            drow = []
            for d, fut in dfuts.items():
                row = fut.result()
                row["domain"] = d
                drow.append(row)
            prow = []
            for p, fut in pfuts.items():
                row = fut.result()
                row["platform"] = p
                row["label"] = PLATFORM_LABELS.get(p, p)
                prow.append(row)
        all_free = all(r["status"] == "available" for r in drow + prow)
        results.append({
            "variation": candidate,
            "domains": drow,
            "platforms": prow,
            "all_confirmed_free": all_free,
            "instagram_unconfirmed": "instagram" in platforms,
            "checked_at": utcnow(),
        })
    return results


def run_check(request: dict[str, Any]) -> dict[str, Any]:
    started = utcnow()
    names = request.get("names") or []
    if not names:
        raise ValueError("At least one candidate name is required.")
    results = []
    for name in names:
        result = check_name(name, request["domains"], request["platforms"], request["countries"])
        if request.get("variations"):
            result["variations"] = check_variations(name, request["domains"], request["platforms"])
        else:
            result["variations"] = []
        results.append(result)
    return {
        "request": request,
        "started_at": started,
        "finished_at": utcnow(),
        "names": results,
        "disclaimer": "Trademark results are a starting point, not legal advice.",
        "method": "Live public lookups (RDAP/WHOIS, platform pages/oEmbed, iTunes Search, Google Play, USPTO, TMview). Results are observations at the stated time and can change at any moment.",
    }


# ---------------------------------------------------------------------------
# Markdown report


def _status_label(status: str) -> str:
    return {"available": "Available", "taken": "Taken", "found": "Found", "none": "None found", "unknown": "Couldn't confirm"}.get(status, status)


def build_report(result: dict[str, Any]) -> str:
    req = result["request"]
    lines = [
        "# Brand Name Availability Report",
        "",
        f"**Checked:** {result['started_at']} → {result['finished_at']} (UTC)",
        "",
        f"**Names:** {', '.join(req.get('names') or [])}",
        f"**Domain extensions:** {', '.join(req.get('domains') or [])}",
        f"**Platforms:** {', '.join(PLATFORM_LABELS.get(p, p) for p in req.get('platforms') or [])}",
        f"**Trademark countries:** {', '.join(req.get('countries') or [])}",
        "",
        "> **Trademark results are a starting point, not legal advice.** "
        "Whether a mark is confusingly similar is a legal judgment. This report flags possible conflicts; it does not decide them.",
        "",
        "## What was checked",
        "",
        "| Name | Domains | Platforms | App stores | Trademark databases |",
        "|---|---|---|---|---|",
    ]
    for item in result["names"]:
        tms = ", ".join(d["database"] for d in item["trademarks"].get("databases") or []) or "none"
        lines.append(
            f"| {item['name']} | {len(item['domains'])} | {len(item['platforms'])} | Apple App Store, Google Play | {tms} |"
        )
    lines.append("")
    for item in result["names"]:
        s = item["summary"]
        lines += [
            f"## {item['name']}",
            "",
            f"**Quick read:** {len(s['domains_available'])} of {len(item['domains'])} domains look free; "
            f"{len(s['handles_available'])} of {len(item['platforms'])} handles confirmed free; "
            f"{s['apps_exact']} exact-name app(s); {s['trademark_conflicts']} possible trademark conflict(s).",
            "",
            "### Domains",
            "",
            "| Domain | Result | Checked at (UTC) | Evidence |",
            "|---|---|---|---|",
        ]
        for d in item["domains"]:
            lines.append(f"| {d['domain']} | **{_status_label(d['status'])}** | {d['checked_at']} | {d['detail']} — {d['source']} |")
        lines += [
            "",
            "### Social handles",
            "",
            f"Exact handle checked: `{item['handle_checked']}`",
            "",
            "| Platform | Handle | Result | Checked at (UTC) | Evidence |",
            "|---|---|---|---|---|",
        ]
        for p in item["platforms"]:
            lines.append(
                f"| {p['label']} | @{p['handle']} | **{_status_label(p['status'])}** | {p['checked_at']} | {p['detail']} — {p['source']} |"
            )
        lines += [
            "",
            "### App stores",
            "",
            "| Store | Exact-name apps | Checked at (UTC) | Detail |",
            "|---|---|---|---|",
        ]
        for a in item["apps"]:
            apps = a.get("apps") or []
            names = ", ".join(f"{x.get('title')} ({x.get('developer') or 'developer unknown'})" for x in apps) or "—"
            lines.append(f"| {a.get('store') or a.get('detail')} | {names} | {a['checked_at']} | {a['detail']} — {a['source']} |")
        lines += [
            "",
            "### Trademarks",
            "",
            "| Database | Result | Checked at (UTC) | Offices / query |",
            "|---|---|---|---|",
        ]
        for db in item["trademarks"].get("databases") or []:
            offices = ", ".join(db.get("offices") or (["US federal"] if "USPTO" in db.get("database", "") else []))
            lines.append(
                f"| {db['database']} | {db['status']} | {db['checked_at']} | {offices or db.get('error') or '—'} |"
            )
        marks = item["trademarks"].get("marks") or []
        if marks:
            lines += [
                "",
                "| Mark | Registration / serial | Status | Owner | Similarity | Database |",
                "|---|---|---|---|---|---|",
            ]
            for m in marks:
                reg = m.get("registration_number") or f"application {m.get('serial_number')}"
                lines.append(
                    f"| {m.get('mark')} | {reg} | {m.get('status') or 'unknown'} | {m.get('owner') or 'unknown'} | {m.get('similarity')} | {m.get('database')} |"
                )
        else:
            lines += ["", "No identical or very similar live marks were found in the database(s) above at check time."]
        lines += ["", f"*{item['trademarks'].get('disclaimer')}*", ""]
        variations = item.get("variations") or []
        if req.get("variations"):
            lines += [
                "### Suggested variations",
                "",
                "| Variation | Domains | Checkable platforms | Confirmed free everywhere checked | Checked at (UTC) |",
                "|---|---|---|---|---|",
            ]
            for v in variations:
                dom = ", ".join(f"{d['domain']}={_status_label(d['status'])}" for d in v["domains"])
                plat = ", ".join(f"{p['label']}={_status_label(p['status'])}" for p in v["platforms"])
                note = "yes" if v["all_confirmed_free"] else "no"
                if v.get("instagram_unconfirmed"):
                    note += " (Instagram unconfirmed)"
                lines.append(f"| {v['variation']} | {dom} | {plat} | {note} | {v['checked_at']} |")
            lines += [
                "",
                "Only variations that are free on every domain and every checkable platform are marked confirmed. "
                "Instagram does not expose a public availability signal, so it is excluded from variation checks and must be confirmed manually.",
                "",
            ]
    lines += [
        "---",
        "",
        "**Method and limits.** Domains are checked through registry RDAP and confirmed with registry WHOIS where available. "
        "Handles are checked only where the platform exposes a public signal; blocked platforms are marked \"couldn't confirm\". "
        "App checks use the public Apple iTunes Search API and the public Google Play search page. "
        "US marks use the public USPTO Trademark Search index; other countries use TMview (EUIPO/TMDN). "
        "Availability can change at any moment, so every result is only accurate at its stated check time. "
        "No domain or handle is registered, reserved or bought by this agent.",
        "",
    ]
    return "\n".join(lines)


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    """Same result without internal caches (all fields are already public)."""
    return result
