"""Small stdlib clients for the Agent Market worker and owner APIs."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


BASE = os.environ.get("MARKET_BASE_URL", "https://market.near.ai").rstrip("/")


class MarketError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _json_request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 120,
) -> tuple[int, dict[str, str], bytes]:
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "VideoClippingAgent/1.0"}
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with opener.open(req, timeout=timeout) as res:
            return res.status, dict(res.headers), res.read()
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        raise MarketError(
            f"{method} {urllib.parse.urlparse(url).path} -> HTTP {exc.code}",
            status=exc.code,
            body=payload.decode("utf-8", "replace")[:2000],
        ) from exc
    except urllib.error.URLError as exc:
        raise MarketError(f"{method} {urllib.parse.urlparse(url).path} -> network error: {exc.reason}") from exc


class MarketAPI:
    """Worker API authenticated with an `aat_` agent token."""

    def __init__(self, token: str, base: str = BASE):
        self.token = token
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def request(self, method: str, path: str, body: Any = None, timeout: float = 120) -> Any:
        status, headers, payload = _json_request(
            self.opener,
            method,
            self.base + path,
            body=body,
            headers=self._headers(),
            timeout=timeout,
        )
        if not payload:
            return {}
        ctype = headers.get("Content-Type", "")
        if "json" in ctype or payload[:1] in (b"{", b"["):
            return json.loads(payload.decode("utf-8", "replace"))
        return payload.decode("utf-8", "replace")

    def get_assignments(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/v1/agents/me/assignments")
        return data.get("assignments", []) if isinstance(data, dict) else []

    def start(self, assignment_id: str) -> Any:
        return self.request("POST", f"/v1/assignments/{assignment_id}/start")

    def message(self, assignment_id: str, body: str) -> Any:
        return self.request("POST", f"/v1/assignments/{assignment_id}/messages", {"body": body[:4000]})

    def progress(self, assignment_id: str, message: str, percent: int | None = None) -> Any:
        payload: dict[str, Any] = {"message": message[:280]}
        if percent is not None:
            payload["percent"] = max(0, min(100, int(percent)))
        return self.request("POST", f"/v1/assignments/{assignment_id}/progress", payload)

    def submit(self, assignment_id: str, markdown: str, files: list[str] | None = None) -> Any:
        payload: dict[str, Any] = {"deliverableMarkdown": markdown}
        if files:
            payload["files"] = files
        return self.request("POST", f"/v1/assignments/{assignment_id}/submit", payload)

    def get_messages(self, assignment_id: str, limit: int = 30) -> list[dict[str, Any]]:
        data = self.request("GET", f"/v1/assignments/{assignment_id}/messages?limit={limit}")
        return data.get("messages", []) if isinstance(data, dict) else []

    def get_attachments(self, job_id: str) -> list[dict[str, Any]]:
        try:
            data = self.request("GET", f"/v1/jobs/{job_id}/attachments")
            if isinstance(data, dict):
                return data.get("attachments", [])
            if isinstance(data, list):
                return data
        except MarketError:
            pass
        return []

    def download(self, url_or_path: str, dest: str) -> str:
        url = url_or_path if url_or_path.startswith("http") else self.base + url_or_path
        req = urllib.request.Request(url, headers={**self._headers(), "Accept": "*/*"})
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            with self.opener.open(req, timeout=120) as res, open(dest, "wb") as out:
                while True:
                    chunk = res.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
        except urllib.error.HTTPError as exc:
            raise MarketError(f"attachment download failed: HTTP {exc.code}") from exc
        return dest

    def upload_file(self, path: str, content_type: str = "video/mp4") -> dict[str, Any]:
        """Reserve, upload and complete a file. Returns fileId and public fileUrl."""
        size = os.path.getsize(path)
        if size <= 0:
            raise MarketError("refusing to upload an empty file")
        if size > 100 * 1024**2:
            raise MarketError("file exceeds the marketplace 100 MiB per-file limit")
        filename = os.path.basename(path)
        data = self.request(
            "POST",
            "/v1/files",
            {"filename": filename, "byteSize": size, "link": True},
        )
        file_id = data.get("fileId")
        upload_url = data.get("uploadUrl")
        direct_url = data.get("directUploadUrl")
        if not file_id or not (upload_url or direct_url):
            raise MarketError(f"file reserve returned an unexpected shape: {sorted(data.keys())}")
        target = direct_url or upload_url
        if target and target.startswith("/"):
            target = self.base + target
        with open(path, "rb") as fh:
            raw = fh.read()
        headers = {"Content-Type": content_type, "Content-Length": str(len(raw))}
        # The same-origin sink accepts a one-shot POST or PUT. Try PUT first.
        req = urllib.request.Request(target, data=raw, method="PUT", headers=headers)
        try:
            with self.opener.open(req, timeout=300) as res:
                res.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 405, 501):
                raise MarketError(f"file upload failed: HTTP {exc.code}", status=exc.code, body=exc.read().decode("utf-8", "replace")[:500]) from exc
            req = urllib.request.Request(target, data=raw, method="POST", headers=headers)
            try:
                with self.opener.open(req, timeout=300) as res:
                    res.read()
            except urllib.error.HTTPError as exc2:
                raise MarketError(f"file upload failed: HTTP {exc2.code}", status=exc2.code, body=exc2.read().decode("utf-8", "replace")[:500]) from exc2
        completed = self.request("POST", f"/v1/files/{file_id}/complete", {})
        file_url = data.get("fileUrl") or completed.get("fileUrl")
        if file_url and file_url.startswith("/"):
            file_url = self.base + file_url
        return {
            "fileId": file_id,
            "fileUrl": file_url,
            "filename": completed.get("filename", filename),
            "byteSize": completed.get("byteSize", size),
            "sha256": completed.get("sha256") or hashlib.sha256(raw).hexdigest(),
        }


class OwnerAPI:
    """Owner session API (cookie + CSRF) for provisioning and webhook updates."""

    def __init__(self, email: str, password: str, base: str = BASE):
        self.base = base.rstrip("/")
        self.email = email
        self.password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""

    def signin(self) -> dict[str, Any]:
        status, headers, payload = _json_request(
            self.opener,
            "POST",
            self.base + "/v1/auth/signin",
            body={"email": self.email, "password": self.password, "remember": True},
            timeout=60,
        )
        data = json.loads(payload.decode())
        self.csrf = data.get("csrf_token", "")
        return data

    def request(self, method: str, path: str, body: Any = None, timeout: float = 120) -> Any:
        headers = {}
        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            headers["X-CSRF-Token"] = self.csrf
        status, ctype, payload = _json_request(
            self.opener,
            method,
            self.base + path,
            body=body,
            headers=headers,
            timeout=timeout,
        )
        if not payload:
            return {}
        if "json" in ctype or payload[:1] in (b"{", b"["):
            return json.loads(payload.decode("utf-8", "replace"))
        return payload.decode("utf-8", "replace")

    def me(self) -> dict[str, Any]:
        return self.request("GET", "/v1/me")

    def list_agents(self, account_id: str) -> list[dict[str, Any]]:
        data = self.request("GET", f"/v1/accounts/{account_id}/agents")
        return data if isinstance(data, list) else data.get("agents", [])

    def get_agent(self, agent_id_or_handle: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/agents/{urllib.parse.quote(agent_id_or_handle)}")

    def check_handle(self, handle: str) -> dict[str, Any]:
        return self.request("GET", f"/v1/accounts/check-slug?slug={urllib.parse.quote(handle)}")

    def create_agent(
        self,
        account_id: str,
        handle: str,
        name: str,
        category: str = "creative",
        runtime: str = "http",
        sla_seconds: int = 21600,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/v1/accounts/{account_id}/agents",
            {
                "handle": handle,
                "name": name,
                "category": category,
                "runtime": runtime,
                "sla_seconds": sla_seconds,
            },
        )

    def patch_agent(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", f"/v1/agents/{agent_id}", payload)

    def list_plans(self, agent_id: str) -> list[dict[str, Any]]:
        data = self.request("GET", f"/v1/agents/{agent_id}/pricing-plans")
        return data if isinstance(data, list) else data.get("plans", [])

    def create_plan(
        self, agent_id: str, price_amount: str, sla_seconds: int | None = None, display_name: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": "per_call", "price_amount": price_amount}
        if sla_seconds:
            payload["sla_seconds"] = sla_seconds
        if display_name:
            payload["display_name"] = display_name
        return self.request("POST", f"/v1/agents/{agent_id}/pricing-plans", payload)

    def make_default_plan(self, plan_id: str) -> Any:
        return self.request("POST", f"/v1/pricing-plans/{plan_id}/make-default", {})

    def create_token(self, agent_id: str, label: str = "runtime") -> dict[str, Any]:
        return self.request("POST", f"/v1/agents/{agent_id}/api-tokens", {"label": label})

    def test_webhook(self, agent_id: str) -> dict[str, Any]:
        return self.request("POST", f"/v1/agents/{agent_id}/webhook/test", {})

    def set_live(self, agent_id: str) -> dict[str, Any]:
        return self.patch_agent(agent_id, {"listing_status": "live"})
