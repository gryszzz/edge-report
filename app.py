#!/usr/bin/env python3
"""Stripe webhook → Resend contact sync + welcome email for Edge Report."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, request, jsonify

import stripe_resend_sync as sync

app = Flask(__name__)
RESEND = "https://api.resend.com"
PRICE_ID = os.environ.get("EDGE_REPORT_PRICE_ID", "price_1UVisUGs0vvfSZLTs3mO1YFV")
FROM = os.environ.get("EDGE_REPORT_FROM", "Edge Report <edge@mail.noisealter.com>")
TRACK_URL = os.environ.get(
    "EDGE_REPORT_TRACK_URL",
    "https://gryszzz.github.io/edge-report/",
)
WELCOME_SUBJECT = os.environ.get(
    "EDGE_REPORT_WELCOME_SUBJECT",
    "Welcome to Edge Report · Issue #001",
)


def _resend_send(to_email: str, subject: str, html: str) -> dict:
    if os.environ.get("EDGE_REPORT_ALLOW_SYNC") != "1":
        return {"dry_run": True, "to": to_email, "subject": subject}
    key = os.environ["RESEND_API_KEY"]
    body = {
        "from": FROM,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        RESEND + "/emails",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _welcome_html() -> str:
    path = Path(__file__).with_name("welcome.html")
    html = path.read_text(encoding="utf-8")
    # Inject track-record link if placeholder present
    return html.replace("{{TRACK_RECORD_URL}}", TRACK_URL)


def _stripe_get(path: str) -> dict:
    key = os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_API_KEY")
    if not key:
        return {}
    req = urllib.request.Request(
        "https://api.stripe.com" + path,
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _resolve_customer_email(customer_id: str) -> str | None:
    if not customer_id:
        return None
    cust = _stripe_get(f"/v1/customers/{customer_id}")
    return cust.get("email")


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "edge-report-stripe-sync"})


@app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get("Stripe-Signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if secret:
        if not sync.verify_stripe_signature(payload, sig, secret):
            return jsonify({"error": "invalid signature"}), 400
    try:
        event = json.loads(payload.decode())
    except Exception:
        return jsonify({"error": "bad json"}), 400

    et = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}

    # Resolve email for subscription events
    if et in ("customer.subscription.updated", "customer.subscription.deleted"):
        email = _resolve_customer_email(str(obj.get("customer") or ""))
        if email:
            obj = dict(obj)
            obj["_resolved_email"] = email
            event = dict(event)
            event["data"] = {"object": obj}

    result = sync.handle_stripe_event(event)

    # Welcome + issue #001 on successful checkout
    if et == "checkout.session.completed" and result.get("dry_run") is not True:
        email = obj.get("customer_details", {}).get("email") or obj.get("customer_email")
        if email and os.environ.get("EDGE_REPORT_SEND_WELCOME", "1") == "1":
            try:
                send_result = _resend_send(email, WELCOME_SUBJECT, _welcome_html())
                result = {"sync": result, "welcome": send_result}
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                result = {"sync": result, "welcome_error": body, "status": e.code}
            except Exception as e:
                result = {"sync": result, "welcome_error": str(e)}
            # Also fire Resend automation event (belt-and-suspenders)
            try:
                event_body = {
                    "event": "edge_report.subscribed",
                    "email": email,
                    "payload": {
                        "email": email,
                        "stripe_customer_id": str(obj.get("customer") or ""),
                    },
                }
                key = os.environ.get("RESEND_API_KEY", "")
                if key and os.environ.get("EDGE_REPORT_ALLOW_SYNC") == "1":
                    data = json.dumps(event_body).encode()
                    req = urllib.request.Request(
                        RESEND + "/events/send",
                        data=data,
                        method="POST",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=30) as r:
                        ev = json.load(r)
                    if isinstance(result, dict):
                        result["automation_event"] = ev
            except Exception as e:
                if isinstance(result, dict):
                    result["automation_event_error"] = str(e)

    return jsonify(result), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
