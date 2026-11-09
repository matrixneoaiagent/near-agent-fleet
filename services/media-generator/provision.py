#!/usr/bin/env python3
"""Provision and maintain the AI Media Generator listing on Agent Market.

Usage:
  python provision.py --provision
  python provision.py --update-webhook URL
  python provision.py --status
  python provision.py --test-webhook

Secrets live in secrets.json (mode 600) and are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from market import OwnerAPI, MarketError  # noqa: E402

ROOT = HERE
CONFIG_PATH = os.path.join(ROOT, "config.json")
SECRETS_PATH = os.path.join(ROOT, "secrets.json")
CREDS_PATH = os.environ.get("NEAR_MARKET_CREDS", "near_market.json")


def load(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save(path: str, data: dict[str, Any], mode: int | None = None) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1)
        fh.write("\n")
    if mode is not None:
        os.chmod(path, mode)


def owner() -> tuple[OwnerAPI, dict[str, Any]]:
    creds = load(CREDS_PATH)
    api = OwnerAPI(creds["email"], creds["password"])
    api.signin()
    return api, creds


def find_agent(api: OwnerAPI, account_id: str, handle: str) -> dict[str, Any] | None:
    for row in api.list_agents(account_id):
        if row.get("handle") == handle:
            return row
    return None


def provision() -> None:
    cfg = load(CONFIG_PATH)
    sec = load(SECRETS_PATH)
    api, creds = owner()
    me = api.me()
    account_id = me["account"]["account_id"]
    handle = cfg.get("handle") or "media-generator"
    name = cfg.get("name") or "AI Image, Video & Voiceover Generator"
    agent = find_agent(api, account_id, handle)
    if agent is None:
        agent = api.create_agent(
            account_id, handle, name, category="creative", runtime="http",
            sla_seconds=int(cfg.get("sla_seconds") or 21600),
        )
        print(f"created draft {agent['agent_id']}")
    else:
        print(f"using existing agent {agent['agent_id']}")
    cfg["agent_id"] = agent["agent_id"]
    cfg.setdefault("listing_url", f"https://market.near.ai/a/{handle}")
    save(CONFIG_PATH, cfg)

    bio_path = os.path.join(ROOT, cfg.get("bio_file") or "BIO.md")
    if not os.path.exists(bio_path):
        raise SystemExit(f"missing bio file: {bio_path}")
    with open(bio_path, "r", encoding="utf-8") as fh:
        bio = fh.read()

    payload: dict[str, Any] = {
        "name": name,
        "description": cfg.get("description") or "Images, short videos and voiceovers from one brief, with exact formats and a provider report.",
        "bio_markdown": bio,
        "tags": cfg.get("tags") or ["image-generation", "video-generation", "voiceover", "creative"],
        "sla_seconds": int(cfg.get("sla_seconds") or 21600),
        "runtime": "http",
        "category": "creative",
    }
    if cfg.get("input_schema"):
        payload["input_schema"] = cfg["input_schema"]
    if cfg.get("output_schema"):
        payload["output_schema"] = cfg["output_schema"]
    webhook_url = cfg.get("public_base_url", "").rstrip("/")
    if webhook_url:
        payload["webhook_url"] = webhook_url + "/webhook"
        payload["webhook_secret"] = sec["webhook_secret"]
    api.patch_agent(agent["agent_id"], payload)
    print("listing fields patched")

    plans = api.list_plans(agent["agent_id"])
    if not plans:
        plan = api.create_plan(
            agent["agent_id"], str(cfg.get("price") or "25.00"),
            sla_seconds=int(cfg.get("sla_seconds") or 21600),
            display_name="Per media job",
        )
        print(f"created pricing plan {plan['pricing_plan_id']}")
    else:
        plan = plans[0]
        if not plan.get("is_default"):
            api.make_default_plan(plan["pricing_plan_id"])
            print("made existing plan default")
        else:
            print("pricing plan already present")

    if not sec.get("agent_token"):
        token = api.create_token(agent["agent_id"], "runtime")
        plaintext = token.get("plaintext")
        if not plaintext:
            raise SystemExit("token endpoint returned no plaintext")
        sec["agent_token"] = plaintext
        save(SECRETS_PATH, sec, 0o600)
        print("issued a new agent token and stored it in secrets.json")
    else:
        print("agent token already stored")

    api.patch_agent(agent["agent_id"], {"listing_status": "live"})
    print("listing status: live")

    if webhook_url:
        try:
            result = api.test_webhook(agent["agent_id"])
            print(f"webhook test: HTTP {result.get('http_status')} in {result.get('duration_ms')} ms")
        except Exception as exc:
            print(f"webhook test failed: {exc}")

    print(f"listing: https://market.near.ai/a/{handle}")
    print(f"agent_id: {agent['agent_id']}")


def update_webhook(url: str) -> None:
    cfg = load(CONFIG_PATH)
    sec = load(SECRETS_PATH)
    if not url.startswith("http"):
        raise SystemExit("webhook URL must start with http")
    base = url.rstrip("/")
    if base.endswith("/webhook"):
        base = base[: -len("/webhook")]
    cfg["public_base_url"] = base
    save(CONFIG_PATH, cfg)
    api, _ = owner()
    agent_id = cfg["agent_id"]
    api.patch_agent(agent_id, {"webhook_url": base + "/webhook", "webhook_secret": sec["webhook_secret"]})
    print(f"webhook updated to {base}/webhook")
    try:
        result = api.test_webhook(agent_id)
        print(f"webhook test: HTTP {result.get('http_status')} in {result.get('duration_ms')} ms")
    except Exception as exc:
        print(f"webhook test failed: {exc}")


def status() -> None:
    cfg = load(CONFIG_PATH)
    sec = load(SECRETS_PATH)
    api, _ = owner()
    agent = api.get_agent(cfg["agent_id"])
    plans = api.list_plans(cfg["agent_id"])
    print(json.dumps({
        "agent_id": agent.get("agent_id"),
        "handle": agent.get("handle"),
        "name": agent.get("name"),
        "category": agent.get("category"),
        "runtime": agent.get("runtime"),
        "listing_status": agent.get("listing_status"),
        "sla_seconds": agent.get("sla_seconds"),
        "webhook_enabled": agent.get("webhook_enabled"),
        "webhook_url": agent.get("webhook_url"),
        "delivered_jobs": agent.get("delivered_jobs"),
        "plans": [{"id": p.get("pricing_plan_id"), "price": p.get("price_amount"),
                   "model": p.get("model"), "default": p.get("is_default"),
                   "status": p.get("listing_status")} for p in plans],
        "token_configured": bool(sec.get("agent_token")),
        "webhook_secret_configured": bool(sec.get("webhook_secret")),
        "listing_url": f"https://market.near.ai/a/{agent.get('handle')}",
    }, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provision", action="store_true")
    parser.add_argument("--update-webhook")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--test-webhook", action="store_true")
    args = parser.parse_args()
    if args.provision:
        provision()
    elif args.update_webhook:
        update_webhook(args.update_webhook)
    elif args.status:
        status()
    elif args.test_webhook:
        cfg = load(CONFIG_PATH)
        api, _ = owner()
        print(api.test_webhook(cfg["agent_id"]))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
