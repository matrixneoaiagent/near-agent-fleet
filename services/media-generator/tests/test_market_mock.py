#!/usr/bin/env python3
"""Exercise the full marketplace worker path against a recording mock.

This proves start -> message -> progress -> upload -> submit are called with
the right shapes without charging a buyer. It is not a paid-market test.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app  # noqa: E402
import pipeline  # noqa: E402


class FakeMarket:
    def __init__(self):
        self.calls = []

    def start(self, aid):
        self.calls.append(("start", aid))

    def message(self, aid, body):
        self.calls.append(("message", aid, body[:120]))

    def progress(self, aid, message, percent=None):
        self.calls.append(("progress", aid, message, percent))

    def get_attachments(self, job_id):
        return []

    def upload_file(self, path, content_type="video/mp4"):
        self.calls.append(("upload", os.path.basename(path), content_type, os.path.getsize(path)))
        return {"fileId": "file-" + os.path.basename(path), "fileUrl": "https://example.invalid/" + os.path.basename(path),
                "filename": os.path.basename(path), "byteSize": os.path.getsize(path), "sha256": "0" * 64}

    def submit(self, aid, markdown, files=None):
        self.calls.append(("submit", aid, len(markdown), list(files or [])))
        return {"ok": True}


def main() -> None:
    fake = FakeMarket()
    app.market_client = lambda: fake  # type: ignore
    aid = "mock-market-path-001"
    row = {
        "assignment": {"assignmentId": aid, "jobId": "job-mock-001", "status": "in_progress",
                       "escrowAmount": "25.00", "escrowToken": "USD", "slaSeconds": 21600,
                       "slaDeadlineAt": "2026-11-09T02:00:00Z", "startedAt": None, "submittedAt": None},
        "job": {"jobId": "job-mock-001", "title": "Mock market path",
                "description": '5-second voiceover, warm female voice, script: "Fresh coffee, poured with care, every morning."',
                "tags": ["voiceover"]},
        "attachments": [], "latestMessage": None,
    }
    app.process_fresh_assignment(row)
    state = pipeline.load_job(aid) or {}
    print(json.dumps({"status": state.get("status"), "calls": fake.calls,
                      "outputs": [(o.get("kind"), o.get("filename"), o.get("status")) for o in state.get("outputs", [])],
                      "report_has_provider": "Microsoft Edge" in (state.get("report_md") or "")}, indent=1))
    ok = (state.get("status") == "submitted"
          and any(c[0] == "start" for c in fake.calls)
          and any(c[0] == "upload" for c in fake.calls)
          and any(c[0] == "submit" and c[3] for c in fake.calls))
    print("MOCK MARKET PATH:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
