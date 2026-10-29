#!/usr/bin/env python3
"""Stripe → Resend contact sync for Edge Report (prepare-only).

Does not register webhooks or call Resend unless EDGE_REPORT_ALLOW_SYNC=1.
Map:
  checkout.session.completed / customer.subscription.updated (active)
    → upsert contact, add to EDGE_REPORT_SEGMENT_ID
  customer.subscription.deleted / status canceled
    → remove from segment or set edge_report_status=canceled

Env:
  RESEND_API_KEY, STRIPE_WEBHOOK_SECRET
  EDGE_REPORT_SEGMENT_ID  (create at launch; do not use General)
  EDGE_REPORT_ALLOW_SYNC=1 to actually mutate Resend
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request

RESEND = "https://api.resend.com"
ALLOW = os.environ.get("EDGE_REPORT_ALLOW_SYNC") == "1"
SEGMENT = os.environ.get("EDGE_REPORT_SEGMENT_ID", "")
PRICE_ID = os.environ.get("EDGE_REPORT_PRICE_ID", "price_1UVisUGs0vvfSZLTs3mO1YFV")


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


def upsert_subscriber(email: str, stripe_customer_id: str, active: bool) -> dict:
    payload = {
        "email": email,
        "first_name": None,
        "unsubscribed": not active,
        "properties": {
            "stripe_customer_id": stripe_customer_id,
            "edge_report_status": "active" if active else "canceled",
        },
    }
    if SEGMENT and active:
        payload["segments"] = [{"id": SEGMENT}]
    return _resend("POST", "/contacts", payload)


def handle_stripe_event(event: dict) -> dict:
    et = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    if et == "checkout.session.completed":
        email = obj.get("customer_details", {}).get("email") or obj.get("customer_email")
        if not email:
            return {"ok": False, "reason": "no email"}
        # optional: verify line items include PRICE_ID via Stripe retrieve
        return upsert_subscriber(email, str(obj.get("customer") or ""), True)
    if et in ("customer.subscription.updated", "customer.subscription.deleted"):
        # Caller should resolve customer email via Stripe API before calling.
        email = obj.get("_resolved_email")
        status = obj.get("status")
        active = status in ("active", "trialing") and et != "customer.subscription.deleted"
        if not email:
            return {"ok": False, "reason": "resolve customer email first"}
        return upsert_subscriber(email, str(obj.get("customer") or ""), active)
    return {"ok": True, "ignored": et}


def verify_stripe_signature(payload: bytes, header: str, secret: str, tolerance: int = 300) -> bool:
    # Stripe-Signature: t=...,v1=...
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    t, v1 = parts.get("t"), parts.get("v1")
    if not t or not v1:
        return False
    if abs(time.time() - int(t)) > tolerance:
        return False
    signed = f"{t}.{payload.decode()}".encode()
    expect = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, v1)


if __name__ == "__main__":
    sample = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "customer": "cus_test",
                "customer_details": {"email": "buyer@example.com"},
            }
        },
    }
    print(json.dumps(handle_stripe_event(sample), indent=2))
    if not ALLOW:
        print("dry-run only; set EDGE_REPORT_ALLOW_SYNC=1 to mutate Resend", file=sys.stderr)
