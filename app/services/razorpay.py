"""Razorpay glue: create a Razorpay order, verify its signatures, translate its webhooks.

Flow
  1. Our order (pending_payment) exists. POST /api/payments/razorpay/order asks Razorpay
     for an order of the same amount and stores its id (order_...) in Order.payment_intent_id.
  2. The browser opens Razorpay Checkout with that id. On success Checkout returns
     (payment_id, order_id, signature); POST /api/payments/razorpay/verify checks
     HMAC_SHA256(order_id + "|" + payment_id, key_secret) and marks the order paid.
  3. Razorpay also calls POST /webhooks/razorpay (X-Razorpay-Signature = HMAC of the raw
     body with the webhook secret). Both paths feed orders.apply_event, which dedupes,
     so whichever arrives second is a harmless no-op, and the webhook alone is enough
     if the buyer closes the tab right after paying.
Amounts: Razorpay wants paise. Our *_cents columns already hold paise for INR.
"""
import hashlib
import hmac
import json

import httpx

from ..config import settings


class RazorpayError(Exception):
    pass


class BadSignature(Exception):
    pass


def _hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


async def create_order(*, amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
    """POST /orders. Returns Razorpay's order JSON (id, amount, currency, ...)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{settings.razorpay_api_base}/orders",
                auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
                json={"amount": amount_paise, "currency": "INR", "receipt": receipt[:40], "notes": notes or {}},
            )
    except httpx.HTTPError as exc:
        raise RazorpayError(f"could not reach Razorpay: {exc!r}")
    if r.status_code != 200:
        raise RazorpayError(f"Razorpay said {r.status_code}: {r.text[:300]}")
    return r.json()


def verify_checkout_signature(razorpay_order_id: str, razorpay_payment_id: str, signature: str) -> None:
    expected = _hmac(settings.razorpay_key_secret, f"{razorpay_order_id}|{razorpay_payment_id}".encode())
    if not hmac.compare_digest(expected, signature or ""):
        raise BadSignature()


def verify_webhook_signature(raw: bytes, signature: str | None) -> None:
    if not settings.razorpay_webhook_secret:
        raise BadSignature()  # no secret configured: refuse everything rather than trust anyone
    if not hmac.compare_digest(_hmac(settings.razorpay_webhook_secret, raw), signature or ""):
        raise BadSignature()


# Razorpay event name -> our gateway-agnostic event type
_EVENT_TYPES = {
    "payment.captured": "payment.succeeded",
    "order.paid": "payment.succeeded",
    "payment.failed": "payment.failed",
    "refund.processed": "payment.refunded",
}


def translate_webhook(raw: bytes, event_id_header: str | None) -> tuple[str, str, str] | None:
    """-> (event_id, our_event_type, razorpay_order_id), or None for events we ignore.
    Raises ValueError / KeyError on a malformed body."""
    body = json.loads(raw)
    our_type = _EVENT_TYPES.get(body["event"])
    if our_type is None:
        return None
    payload = body["payload"]
    # Every one of these events carries the payment entity (refund.processed carries
    # both refund and payment); order.paid carries the order entity too.
    payment = payload.get("payment", {}).get("entity", {})
    order_id = payment.get("order_id") or payload.get("order", {}).get("entity", {}).get("id")
    if not order_id:
        raise KeyError("order_id")
    # The header is Razorpay's unique delivery id; fall back to a stable id built from the body.
    event_id = event_id_header or f"rzp_{body['event']}_{payment.get('id', '')}_{payload.get('refund', {}).get('entity', {}).get('id', '')}"
    return event_id, our_type, order_id