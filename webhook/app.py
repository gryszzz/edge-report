#!/usr/bin/env python3
"""Stripe webhook → Resend multi-offer sync (Edge / Scout / Noise Alter)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, request, jsonify

import stripe_resend_sync as sync

app = Flask(__name__)
RESEND = "https://api.resend.com"
ALLOW = os.environ.get("EDGE_REPORT_ALLOW_SYNC") == "1"

EDGE_FROM = os.environ.get("EDGE_REPORT_FROM", "Edge Report <edge@mail.noisealter.com>")
SCOUT_FROM = os.environ.get("SCOUT_DIGEST_FROM", "Scout Digest <scout@mail.noisealter.com>")
NA_FROM = os.environ.get("NA_PRESETS_FROM", "Noise Alter <orders@mail.noisealter.com>")

TRACK_URL = os.environ.get("EDGE_REPORT_TRACK_URL", "https://gryszzz.github.io/edge-report/")
SCOUT_ISSUE_URL = os.environ.get(
    "SCOUT_DIGEST_ISSUE_URL",
    "https://gryszzz.github.io/scout-digest/issues/001.html",
)
NA_LANDING_URL = os.environ.get(
    "NA_PRESETS_LANDING_URL",
    "https://gryszzz.github.io/noise-alter-presets/",
)

# Only this address may receive real sends from /test/send-sample
BOT_INBOX = os.environ.get("CABAL_BOT_INBOX", "syzzzz@mail.grokbot.com")


def _resend_send(from_addr: str, to_email: str, subject: str, html: str) -> dict:
    if not ALLOW:
        return {"dry_run": True, "from": from_addr, "to": to_email, "subject": subject}
    key = os.environ["RESEND_API_KEY"]
    body = {"from": from_addr, "to": [to_email], "subject": subject, "html": html}
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


def _template(name: str) -> str:
    path = Path(__file__).with_name(name)
    return path.read_text(encoding="utf-8")


def _welcome_for(kind: str) -> tuple[str, str, str]:
    """Return (from, subject, html) for welcome kind."""
    if kind == "edge":
        html = _template("welcome.html").replace("{{TRACK_RECORD_URL}}", TRACK_URL)
        return EDGE_FROM, "Welcome to Edge Report · Issue #001", html
    if kind == "scout":
        html = _template("welcome_scout.html").replace("{{ISSUE_URL}}", SCOUT_ISSUE_URL)
        return SCOUT_FROM, "Welcome to Scout Research Digest · Issue #001", html
    if kind == "na_preorder":
        html = _template("welcome_na_preorder.html").replace("{{LANDING_URL}}", NA_LANDING_URL)
        return NA_FROM, "Noise Alter · Signal Bank 001 preorder confirmed", html
    raise ValueError(f"unknown welcome kind: {kind}")


def _stripe_get(path: str, query: dict | None = None) -> dict:
    key = os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_API_KEY")
    if not key:
        return {}
    url = "https://api.stripe.com" + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _resolve_customer_email(customer_id: str) -> str | None:
    if not customer_id:
        return None
    cust = _stripe_get(f"/v1/customers/{customer_id}")
    return cust.get("email")


def _price_ids_from_checkout_session(session_id: str) -> list[str]:
    if not session_id:
        return []
    data = _stripe_get(
        f"/v1/checkout/sessions/{session_id}/line_items",
        {"limit": "20"},
    )
    ids = []
    for item in data.get("data") or []:
        price = item.get("price") or {}
        if isinstance(price, dict) and price.get("id"):
            ids.append(price["id"])
    return ids


def _price_ids_from_subscription(obj: dict) -> list[str]:
    ids = []
    items = ((obj.get("items") or {}).get("data")) or []
    for it in items:
        price = it.get("price") or {}
        if isinstance(price, dict) and price.get("id"):
            ids.append(price["id"])
        elif isinstance(price, str):
            ids.append(price)
    return ids


def _send_welcomes(email: str, matched: list[dict]) -> list[dict]:
    out = []
    for m in matched:
        kind = m.get("welcome")
        if not kind:
            continue
        if os.environ.get("EDGE_REPORT_SEND_WELCOME", "1") != "1":
            out.append({"offer": m.get("offer"), "skipped": "SEND_WELCOME off"})
            continue
        try:
            frm, subject, html = _welcome_for(kind)
            sent = _resend_send(frm, email, subject, html)
            out.append({"offer": m.get("offer"), "welcome": kind, "result": sent})
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            out.append({"offer": m.get("offer"), "welcome_error": body, "status": e.code})
        except Exception as e:
            out.append({"offer": m.get("offer"), "welcome_error": str(e)})
    return out


@app.get("/health")
def health():
    return jsonify(
        {
            "ok": True,
            "service": "edge-report-stripe-sync",
            "offers": list({o["key"] for o in sync.OFFERS.values()}),
            "allow_sync": ALLOW,
        }
    )


@app.post("/test/dry-route")
def test_dry_route():
    """Safe local/staging helper: route sample events without requiring Stripe signature.
    Never sends email. Always forces dry-run style response of routing decisions.
    Requires header X-Test-Token == CABAL_TEST_TOKEN when that env is set.
    """
    expected = os.environ.get("CABAL_TEST_TOKEN", "")
    if expected and request.headers.get("X-Test-Token") != expected:
        return jsonify({"error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    offer = body.get("offer", "scout")  # edge|scout|na
    price_map = {
        "edge": sync.EDGE_PRICE,
        "scout": sync.SCOUT_PRICE,
        "na": sync.NA_PRICE,
    }
    price_id = price_map.get(offer)
    if not price_id:
        return jsonify({"error": "offer must be edge|scout|na"}), 400
    email = body.get("email", "buyer@example.com")
    # Hard block: never use a real non-bot address for anything that could mutate when ALLOW=1
    # This endpoint always calls handle with dry semantics by temporarily... we just don't send.
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "customer": "cus_test_dry",
                "customer_details": {"email": email},
            }
        },
    }
    # Always dry: call sync handlers but do not call _send_welcomes for non-bot emails
    result = sync.handle_stripe_event(event, [price_id])
    welcomes = []
    for m in result.get("matched") or []:
        kind = m.get("welcome")
        frm, subject, _html = _welcome_for(kind)
        welcomes.append(
            {
                "offer": m.get("offer"),
                "from": frm,
                "subject": subject,
                "would_send": False,
                "note": "dry-route never sends",
            }
        )
    return jsonify({"dry": True, "sync": result, "welcomes": welcomes}), 200


@app.post("/test/send-sample")
def test_send_sample():
    """Send one real sample email ONLY to the bot inbox. Requires ALLOW_SYNC=1 and token."""
    expected = os.environ.get("CABAL_TEST_TOKEN", "")
    if expected and request.headers.get("X-Test-Token") != expected:
        return jsonify({"error": "unauthorized"}), 401
    if not ALLOW:
        return jsonify({"error": "ALLOW_SYNC off — refusing send"}), 403
    body = request.get_json(silent=True) or {}
    offer = body.get("offer", "scout")
    to = body.get("to", BOT_INBOX)
    if to.lower() != BOT_INBOX.lower():
        return jsonify({"error": f"refusing send to non-bot inbox; only {BOT_INBOX} allowed"}), 403
    kind_map = {"edge": "edge", "scout": "scout", "na": "na_preorder"}
    kind = kind_map.get(offer)
    if not kind:
        return jsonify({"error": "offer must be edge|scout|na"}), 400
    frm, subject, html = _welcome_for(kind)
    try:
        result = _resend_send(frm, to, f"[TEST] {subject}", html)
        return jsonify({"ok": True, "to": to, "result": result}), 200
    except urllib.error.HTTPError as e:
        return jsonify({"error": e.read().decode(errors="replace"), "status": e.code}), 502


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
    price_ids: list[str] = []

    if et == "checkout.session.completed":
        price_ids = _price_ids_from_checkout_session(str(obj.get("id") or ""))
    elif et in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        email = _resolve_customer_email(str(obj.get("customer") or ""))
        if email:
            obj = dict(obj)
            obj["_resolved_email"] = email
            event = dict(event)
            event["data"] = {"object": obj}
        price_ids = _price_ids_from_subscription(obj)

    result = sync.handle_stripe_event(event, price_ids)

    if et == "checkout.session.completed" and result.get("matched"):
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
        if email:
            welcomes = _send_welcomes(email, result["matched"])
            result = {"sync": result, "welcomes": welcomes}
            # Edge-only automation event (existing Resend automation)
            for m in result["sync"].get("matched") or []:
                if m.get("offer") != "edge_report":
                    continue
                try:
                    key = os.environ.get("RESEND_API_KEY", "")
                    if key and ALLOW:
                        event_body = {
                            "event": "edge_report.subscribed",
                            "email": email,
                            "payload": {
                                "email": email,
                                "stripe_customer_id": str(obj.get("customer") or ""),
                            },
                        }
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
                            result["automation_event"] = json.load(r)
                except Exception as e:
                    result["automation_event_error"] = str(e)

    return jsonify(result), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
