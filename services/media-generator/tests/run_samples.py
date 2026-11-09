#!/usr/bin/env python3
"""Run the four sample scenarios through the public tunnel and save evidence.

No secrets are printed. Files land in test-output/<scenario>/.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "test-output")
BASE = json.load(open(os.path.join(ROOT, "state", "tunnel.json")))["url"].rstrip("/")
KEY = json.load(open(os.path.join(ROOT, "secrets.json")))["test_key"]

VOICE_SCRIPT = (
    "Welcome to the studio. Today we are tasting a fresh roast, brewed slowly and poured with care. "
    "Notice the aroma first: warm cocoa, toasted almond, and a hint of orange. Take a small sip and let it settle. "
    "The finish is smooth, rounded, and quietly sweet, with just enough brightness to keep you coming back for another cup."
)

SCENARIOS = [
    ("images", {
        "brief": "3 square product photos of a ceramic mug on a wooden table",
        "media_types": ["image"], "count": 3, "aspect": "1:1",
    }),
    ("video", {
        "brief": "8-second vertical video of coffee being poured into a cup, stock is fine if AI generation is unavailable",
        "media_types": ["video"], "aspect": "9:16", "duration": 8, "allow_stock_video": True,
    }),
    ("voiceover", {
        "brief": "30-second voiceover, warm female voice",
        "media_types": ["voiceover"], "duration": 30, "script": VOICE_SCRIPT, "voice": "warm_female",
    }),
    ("decline", {
        "brief": "Image of a well-known celebrity holding the product",
        "media_types": ["image"], "count": 1, "aspect": "1:1",
    }),
]


def api(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET",
                                 headers={"content-type": "application/json", "X-Test-Key": KEY})
    with urllib.request.urlopen(req, timeout=60) as res:
        return json.loads(res.read().decode())


def download(url: str, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as res, open(path, "wb") as fh:
        while True:
            chunk = res.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    summary = {"base": BASE, "scenarios": []}
    for name, body in SCENARIOS:
        print(f"== {name}", flush=True)
        job = api("/v1/hire", body)
        jid = job["job_id"]
        state: dict = {}
        for _ in range(150):
            time.sleep(3)
            state = api(f"/v1/jobs/{jid}")
            if state.get("status") in ("ready", "failed", "declined"):
                break
        row = {
            "scenario": name, "job_id": jid, "status": state.get("status"),
            "report_url": f"{BASE}/v1/jobs/{jid}/report",
            "request": body,
            "outputs": [],
            "declines": state.get("declines", []),
            "provider_notes": state.get("provider_notes", []),
        }
        folder = os.path.join(OUT, name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "job.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, ensure_ascii=False)
        try:
            with urllib.request.urlopen(f"{BASE}/v1/jobs/{jid}/report", timeout=60) as res:
                report = res.read().decode("utf-8", "replace")
            with open(os.path.join(folder, "report.md"), "w", encoding="utf-8") as fh:
                fh.write(report)
        except Exception as exc:
            report = f"report fetch failed: {exc}"
        for o in state.get("outputs", []):
            if o.get("status") != "ok":
                continue
            filename = o["filename"]
            try:
                download(f"{BASE}/v1/jobs/{jid}/files/{filename}", os.path.join(folder, filename))
                local = os.path.join(folder, filename)
            except Exception as exc:
                local = ""
                o["download_error"] = str(exc)
            row["outputs"].append({
                "kind": o.get("kind"), "filename": filename, "provider": o.get("provider"),
                "model": o.get("model"), "width": o.get("width"), "height": o.get("height"),
                "duration": o.get("duration"), "size": o.get("size"), "sha256": o.get("sha256"),
                "local": local, "public_url": f"{BASE}/v1/jobs/{jid}/files/{filename}",
                "note": o.get("note"),
            })
        summary["scenarios"].append(row)
        print(json.dumps(row, indent=1)[:1800], flush=True)
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1, ensure_ascii=False)
    print("wrote", os.path.join(OUT, "summary.json"))


if __name__ == "__main__":
    main()
