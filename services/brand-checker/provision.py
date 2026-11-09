#!/usr/bin/env python3
"""Provision and maintain the Brand Name Availability Checker listing.

Usage:
  python provision.py --provision
  python provision.py --update-webhook URL
  python provision.py --status
  python provision.py --test-webhook
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from market import MarketError, OwnerAPI  # noqa: E402

CONFIG_PATH = os.path.join(HERE, "config.json")
SECRETS_PATH = os.path.join(HERE, "secrets.json")
CREDS_PATH = os.environ.get("NEAR_MARKET_CREDS", "near_market.json")


def load(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


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


def ensure_secret() -> dict[str, Any]:
    sec = load(SECRETS_PATH)
    if not sec.get("webhook_secret"):
        sec["webhook_secret"] = secrets.token_hex(32)
        save(SECRETS_PATH, sec, 0o600)
    return sec


def provision() -> None:
    cfg = load(CONFIG_PATH)
    sec = ensure_secret()
    api, _ = owner()
    me = api.me()
    account_id = me["account"]["account_id"]
    handle = cfg.get("handle") or "brand-name-checker"
    name = cfg.get("name") or "Brand Name Availability Checker"
    agent = find_agent(api, account_id, handle)
    if agent is None:
        try:
            slug = api.check_handle(handle)
            print("slug check:", json.dumps(slug))
        except Exception:
            pass
        agent = api.create_agent(
            account_id,
            handle,
            name,
            category=str(cfg.get("category") or "research"),
            runtime="http",
            sla_seconds=int(cfg.get("sla_seconds") or 3600),
        )
        print(f"created draft {agent['agent_id']}")
    else:
        print(f"using existing agent {agent['agent_id']}")
    cfg["agent_id"] = agent["agent_id"]
    cfg["listing_url"] = f"https://market.near.ai/a/{handle}"
    save(CONFIG_PATH, cfg)

    payload: dict[str, Any] = {
        "name": name,
        "description": cfg.get("description"),
        "bio_markdown": cfg.get("bio_markdown"),
        "tags": cfg.get("tags") or ["brand", "naming", "domain-check", "trademark"],
        "sla_seconds": int(cfg.get("sla_seconds") or 3600),
        "runtime": "http",
        "category": str(cfg.get("category") or "research"),
    }
    if cfg.get("input_schema"):
        payload["input_schema"] = cfg["input_schema"]
    if cfg.get("output_schema"):
        payload["output_schema"] = cfg["output_schema"]
    base = str(cfg.get("public_base_url") or "").rstrip("/")
    if base:
        payload["webhook_url"] = base + "/webhook"
        payload["webhook_secret"] = sec["webhook_secret"]
    api.patch_agent(agent["agent_id"], payload)
    print("listing fields patched")

    plans = api.list_plans(agent["agent_id"])
    if not plans:
        plan = api.create_plan(
            agent["agent_id"],
            str(cfg.get("price") or "5.00"),
            sla_seconds=int(cfg.get("sla_seconds") or 3600),
            display_name="Per brand check (1–5 names)",
        )
        print(f"created pricing plan {plan.get('pricing_plan_id')}")
    else:
        plan = plans[0]
        print(f"pricing plan already present: {plan.get('pricing_plan_id')}")

    if not sec.get("agent_token"):
        token = api.create_token(agent["agent_id"], "runtime")
        plaintext = token.get("plaintext")
        if not plaintext:
            raise SystemExit("token endpoint returned no plaintext")
        sec["agent_token"] = plaintext
        save(SECRETS_PATH, sec, 0o600)
        print("issued agent token and stored it in secrets.json")
    else:
        print("agent token already stored")

    try:
        api.patch_agent(agent["agent_id"], {"listing_status": "live"})
        print("listing status: live")
    except MarketError as exc:
        print(f"could not publish: {exc} {exc.body or ''}")
        raise

    if base:
        try:
            result = api.test_webhook(agent["agent_id"])
            print(f"webhook test: HTTP {result.get('http_status')} in {result.get('duration_ms')} ms")
        except Exception as exc:
            print(f"webhook test failed: {exc}")

    print(f"listing: https://market.near.ai/a/{handle}")
    print(f"agent_id: {agent['agent_id']}")


def update_webhook(url: str) -> None:
    cfg = load(CONFIG_PATH)
    sec = ensure_secret()
    if not url.startswith("http"):
        raise SystemExit("webhook URL must start with http")
    base = url.rstrip("/")
    if base.endswith("/webhook"):
        base = base[: -len("/webhook")]
    cfg["public_base_url"] = base
    save(CONFIG_PATH, cfg)
    api, _ = owner()
    api.patch_agent(cfg["agent_id"], {"webhook_url": base + "/webhook", "webhook_secret": sec["webhook_secret"]})
    print(f"webhook updated to {base}/webhook")
    try:
        result = api.test_webhook(cfg["agent_id"])
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
        "plans": [
            {
                "id": p.get("pricing_plan_id"),
                "price": p.get("price_amount"),
                "model": p.get("model"),
                "default": p.get("is_default"),
            }
            for p in plans
        ],
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
