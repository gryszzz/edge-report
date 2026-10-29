#!/usr/bin/env python3
"""Stripe → Resend multi-offer sync. Routes by Stripe price ID.

Offers:
  Edge Report          price_1UVisUGs0vvfSZLTs3mO1YFV  segment + welcome
  Scout Research Digest price_1UVjM7Gs0vvfSZLTgKjMSgAW  segment + welcome
  Noise Alter Signal Bank 001 price_1UVjM4Gs0vvfSZLTsMamI6v1  contact prop + preorder confirm

Mutates Resend only when EDGE_REPORT_ALLOW_SYNC=1 (existing live flag).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from typing import Any

RESEND = "https://api.resend.com"
ALLOW = os.environ.get("EDGE_REPORT_ALLOW_SYNC") == "1"

EDGE_PRICE = os.environ.get("EDGE_REPORT_PRICE_ID", "price_1UVisUGs0vvfSZLTs3mO1YFV")
SCOUT_PRICE = os.environ.get("SCOUT_DIGEST_PRICE_ID", "price_1UVjM7Gs0vvfSZLTgKjMSgAW")
NA_PRICE = os.environ.get("NA_PRESETS_PRICE_ID", "price_1UVjM4Gs0vvfSZLTsMamI6v1")

EDGE_SEGMENT = os.environ.get("EDGE_REPORT_SEGMENT_ID", "ae6e93e5-5a40-4e00-93eb-65ca2a4f2752")
SCOUT_SEGMENT = os.environ.get("SCOUT_DIGEST_SEGMENT_ID", "234ef1ed-0fec-4731-815f-69bcdc6d3a26")

OFFERS = {
    EDGE_PRICE: {
        "key": "edge_report",
        "kind": "subscription",
        "segment_id": EDGE_SEGMENT,
        "status_prop": "edge_report_status",
        "welcome": "edge",
    },
    SCOUT_PRICE: {
        "key": "scout_digest",
        "kind": "subscription",
        "segment_id": SCOUT_SEGMENT,
        "status_prop": "scout_digest_status",
        "welcome": "scout",
    },
    NA_PRICE: {
        "key": "na_presets",
        "kind": "one_time",
        "segment_id": None,  # Resend free plan segment cap
        "status_prop": "na_presets_status",
        "status_value_active": "preorder",
        "welcome": "na_preorder",
    },
}


def _resend(method: str, path: str, body: dict | None = None) -> dict:
    if not ALLOW:
        return {"dry_run": True, "method": method, "path": path, "body": body}
    key = os.environ["RESEND_API_KEY"]
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        RESEND + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def upsert_contact(
    email: str,
    stripe_customer_id: str,
    offer: dict,
    active: bool,
) -> dict:
    status_prop = offer["status_prop"]
    if offer["key"] == "na_presets":
        status = offer.get("status_value_active", "preorder") if active else "canceled"
    else:
        status = "active" if active else "canceled"
    payload: dict[str, Any] = {
        "email": email,
        "unsubscribed": not active and offer["kind"] == "subscription",
        "properties": {
            "stripe_customer_id": stripe_customer_id,
            status_prop: status,
        },
    }
    seg = offer.get("segment_id")
    if seg and active:
        payload["segments"] = [{"id": seg}]
    return _resend("POST", "/contacts", payload)


def verify_stripe_signature(payload: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        return False
    if abs(time.time() - int(t)) > tolerance:
        return False
    signed = f"{t}.{payload.decode()}".encode()
    expect = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, v1)


def offers_for_price_ids(price_ids: list[str]) -> list[dict]:
    out = []
    seen = set()
    for pid in price_ids:
        offer = OFFERS.get(pid)
        if offer and offer["key"] not in seen:
            out.append({**offer, "price_id": pid})
            seen.add(offer["key"])
    return out


def handle_checkout_completed(email: str, customer_id: str, price_ids: list[str]) -> dict:
    matched = offers_for_price_ids(price_ids)
    if not matched:
        return {"ok": True, "ignored": "no matching offer price", "price_ids": price_ids}
    results = []
    for offer in matched:
        results.append(
            {
                "offer": offer["key"],
                "price_id": offer["price_id"],
                "welcome": offer["welcome"],
                "sync": upsert_contact(email, customer_id, offer, True),
            }
        )
    return {"ok": True, "matched": results}


def handle_subscription_event(
    email: str,
    customer_id: str,
    price_ids: list[str],
    active: bool,
) -> dict:
    # Only subscription offers
    matched = [o for o in offers_for_price_ids(price_ids) if o["kind"] == "subscription"]
    if not matched:
        return {"ok": True, "ignored": "no matching subscription offer", "price_ids": price_ids}
    results = []
    for offer in matched:
        results.append(
            {
                "offer": offer["key"],
                "price_id": offer["price_id"],
                "active": active,
                "sync": upsert_contact(email, customer_id, offer, active),
            }
        )
    return {"ok": True, "matched": results}


def handle_stripe_event(event: dict, price_ids: list[str] | None = None) -> dict:
    """Handle event. Caller should pass resolved price_ids when available."""
    et = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    price_ids = list(price_ids or obj.get("_price_ids") or [])

    if et == "checkout.session.completed":
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
        if not email:
            return {"ok": False, "reason": "no email"}
        return handle_checkout_completed(email, str(obj.get("customer") or ""), price_ids)

    if et in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        email = obj.get("_resolved_email")
        status = obj.get("status")
        active = status in ("active", "trialing") and et != "customer.subscription.deleted"
        if not email:
            return {"ok": False, "reason": "resolve customer email first"}
        if not price_ids:
            # pull from items.data[].price.id if present on object
            items = ((obj.get("items") or {}).get("data")) or []
            for it in items:
                price = it.get("price") or {}
                if isinstance(price, dict) and price.get("id"):
                    price_ids.append(price["id"])
                elif isinstance(price, str):
                    price_ids.append(price)
        return handle_subscription_event(email, str(obj.get("customer") or ""), price_ids, active)

    return {"ok": True, "ignored": et}


if __name__ == "__main__":
    # Local dry-run routing demos
    samples = [
        (
            "edge",
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "customer": "cus_test",
                        "customer_details": {"email": "buyer@example.com"},
                    }
                },
            },
            [EDGE_PRICE],
        ),
        (
            "scout",
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "customer": "cus_test",
                        "customer_details": {"email": "buyer@example.com"},
                    }
                },
            },
            [SCOUT_PRICE],
        ),
        (
            "na",
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "customer": "cus_test",
                        "customer_details": {"email": "buyer@example.com"},
                    }
                },
            },
            [NA_PRICE],
        ),
        (
            "unknown",
            {
                "type": "checkout.session.completed",
                "data": {
                    "object": {
                        "customer": "cus_test",
                        "customer_details": {"email": "buyer@example.com"},
                    }
                },
            },
            ["price_unknown"],
        ),
    ]
    for name, ev, pids in samples:
        print(f"=== {name} ===")
        print(json.dumps(handle_stripe_event(ev, pids), indent=2))
    if not ALLOW:
        print("dry-run only; set EDGE_REPORT_ALLOW_SYNC=1 to mutate Resend", file=sys.stderr)
