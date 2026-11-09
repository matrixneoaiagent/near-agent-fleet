"""Unit tests for the NEAR Agent Fleet control plane.

Run with no network:
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fleet  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def response(body: bytes, status: int = 200, content_type: str = "application/json") -> dict:
    return {
        "ok": 200 <= status < 400,
        "status": status,
        "content_type": content_type,
        "body": body,
        "latency_ms": 12,
        "error": "",
    }


class ListingParserTests(unittest.TestCase):
    def test_extracts_service_jsonld(self) -> None:
        document = (FIXTURES / "listing.html").read_text(encoding="utf-8")
        service = fleet.parse_listing_jsonld(document)
        self.assertIsNotNone(service)
        assert service is not None
        self.assertEqual(service["name"], "Brand Name Availability Checker")
        self.assertEqual(service["price"], "5.00")
        self.assertEqual(service["service_type"], "Research")

    def test_extracts_delivered_jobs(self) -> None:
        document = (FIXTURES / "listing.html").read_text(encoding="utf-8")
        self.assertEqual(fleet.parse_delivered_jobs(document), "0")

    def test_ignores_non_json_script_blocks(self) -> None:
        self.assertIsNone(fleet.parse_listing_jsonld("<html><script>alert(1)</script></html>"))


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = fleet.load_catalog()

    def test_video_brief_routes_to_clipper(self) -> None:
        result = fleet.route_brief(
            "Make five vertical clips with captions from this 40 minute podcast", self.catalog
        )
        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["recommendation"]["id"], "video-clipper")
        self.assertIn("captions", result["recommendation"]["matched_keywords"])

    def test_brand_brief_routes_to_checker(self) -> None:
        result = fleet.route_brief(
            "Check the brand name Vellumforge on .studio, .dev and the X handle", self.catalog
        )
        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["recommendation"]["id"], "brand-checker")
        self.assertEqual(result["recommendation"]["price_usd"], "5.00")

    def test_media_brief_routes_to_generator(self) -> None:
        result = fleet.route_brief(
            "Generate three square product photos and a short voiceover for a ceramic mug",
            self.catalog,
        )
        self.assertFalse(result["needs_clarification"])
        self.assertEqual(result["recommendation"]["id"], "media-generator")

    def test_empty_brief_needs_clarification(self) -> None:
        result = fleet.route_brief("", self.catalog)
        self.assertTrue(result["needs_clarification"])
        self.assertIsNone(result["recommendation"])

    def test_unknown_brief_needs_clarification(self) -> None:
        result = fleet.route_brief("what is the weather in Chisinau", self.catalog)
        self.assertTrue(result["needs_clarification"])
        self.assertEqual(result["confidence"], 0.0)

    def test_quote_contains_hire_link_and_sla(self) -> None:
        result = fleet.route_brief("I need a logo and a 9:16 image", self.catalog)
        rec = result["recommendation"]
        self.assertEqual(rec["id"], "media-generator")
        self.assertTrue(rec["listing"].startswith("https://market.near.ai/a/"))
        self.assertEqual(rec["sla"], "6 hours")


class CatalogTests(unittest.TestCase):
    def test_three_agents_all_https(self) -> None:
        catalog = fleet.load_catalog()
        self.assertEqual(len(catalog["agents"]), 3)
        for agent in catalog["agents"]:
            for key in ("listing", "health", "card", "test_url"):
                self.assertTrue(agent[key].startswith("https://"), (agent["id"], key))

    def test_ids_unique_and_prices_are_decimal_strings(self) -> None:
        catalog = fleet.load_catalog()
        ids = [a["id"] for a in catalog["agents"]]
        self.assertEqual(len(ids), len(set(ids)))
        for agent in catalog["agents"]:
            self.assertRegex(agent["price_usd"], r"^\d+\.\d{2}$")


class CheckAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent = fleet.load_catalog()["agents"][0]

    def _fake(self, url: str, method: str = "GET", payload=None, timeout: float = 25.0):
        if url == self.agent["listing"]:
            document = (FIXTURES / "listing.html").read_text(encoding="utf-8").encode("utf-8")
            return response(document, content_type="text/html; charset=utf-8")
        if url == self.agent["health"]:
            return response(json.dumps({"ok": True, "service": "brand-name-checker", "uptime_seconds": 10}).encode())
        if url == self.agent["card"]:
            return response((FIXTURES / "agent-card.json").read_bytes())
        return response(b"# report\n" + b"x" * 200, content_type="text/markdown")

    def test_all_checks_live(self) -> None:
        with mock.patch.object(fleet, "http_request", side_effect=self._fake):
            result = fleet.check_agent(self.agent)
        self.assertEqual(result["status"], "live")
        self.assertEqual(result["checks_passed"], 4)
        self.assertEqual(result["delivered_jobs"], "0")

    def test_health_failure_is_degraded_not_down(self) -> None:
        def degraded(url: str, method: str = "GET", payload=None, timeout: float = 25.0):
            if url == self.agent["health"]:
                return response(b'{"ok": false}', status=503)
            return self._fake(url, method, payload, timeout)

        with mock.patch.object(fleet, "http_request", side_effect=degraded):
            result = fleet.check_agent(self.agent)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["checks_passed"], 3)

    def test_listing_failure_is_down(self) -> None:
        def down(url: str, method: str = "GET", payload=None, timeout: float = 25.0):
            if url == self.agent["listing"]:
                return response(b"", status=500)
            return self._fake(url, method, payload, timeout)

        with mock.patch.object(fleet, "http_request", side_effect=down):
            result = fleet.check_agent(self.agent)
        self.assertEqual(result["status"], "down")


class OutputTests(unittest.TestCase):
    def test_markdown_contains_every_agent(self) -> None:
        catalog = fleet.load_catalog()
        status = {
            "fleet": catalog["fleet"],
            "generated_at": "2026-11-09T00:00:00Z",
            "duration_ms": 1,
            "summary": {"agents": 3, "live": 3, "degraded": 0, "down": 0, "checks_passed": 12, "checks_total": 12},
            "agents": [
                {
                    "name": a["name"],
                    "listing": a["listing"],
                    "price_usd": a["price_usd"],
                    "sla": a["sla"],
                    "status": "live",
                    "checks_passed": 4,
                    "checks_total": 4,
                    "delivered_jobs": "0",
                    "checks": [{"ok": True, "label": "x", "detail": "y", "latency_ms": 1}],
                }
                for a in catalog["agents"]
            ],
        }
        markdown = fleet.render_markdown(status)
        for agent in catalog["agents"]:
            self.assertIn(agent["name"], markdown)
        self.assertIn("3/3 live", markdown)


class SecretHygieneTests(unittest.TestCase):
    PATTERNS = [
        re.compile(r"aat_[A-Za-z0-9_-]{12,}"),
        re.compile(r"whsec_[A-Za-z0-9_-]{8,}"),
        re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ]

    def test_no_credential_shapes_in_repository(self) -> None:
        skip = {".git", "__pycache__", ".venv", "node_modules"}
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in skip for part in path.parts):
                continue
            if path.suffix in {".mp4", ".png", ".jpg", ".webp", ".zip"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                continue
            for pattern in self.PATTERNS:
                if pattern.search(text):
                    offenders.append(f"{path.relative_to(ROOT)} matches {pattern.pattern}")
        self.assertEqual(offenders, [])

    def test_gitignore_blocks_runtime_state(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for item in ("secrets.json", "state/", "logs/", ".venv/"):
            self.assertIn(item, text)


if __name__ == "__main__":
    unittest.main()
