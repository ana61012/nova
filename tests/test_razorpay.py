import hashlib
import hmac
import json

import pytest

from app.config import settings
from app.services import razorpay as rzp

from .conftest import buy_to_order, signup, stock

KEY_SECRET, WH_SECRET = "rzp_test_secret", "whsec_rzp"


def _sig(secret: str, msg: bytes) -> str:
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


@pytest.fixture
def razorpay_on(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", KEY_SECRET)
    monkeypatch.setattr(settings, "razorpay_webhook_secret", WH_SECRET)
    calls = []

    async def fake_create(*, amount_paise, receipt, notes=None):
        calls.append({"amount": amount_paise, "receipt": receipt})
        return {"id": f"order_fake{len(calls)}", "amount": amount_paise, "currency": "INR"}

    monkeypatch.setattr(rzp, "create_order", fake_create)
    return calls


async def _start(api, name="rz"):
    h = await signup(api, name)
    order = await buy_to_order(api, h)
    r = await api.post("/api/payments/razorpay/order", json={"order_id": order["id"]}, headers=h)
    assert r.status_code == 200, r.text
    return h, order, r.json()


def _webhook(event: str, order_id: str, payment_id="pay_1", event_id="evt_1", refund_id=None):
    payload = {"payment": {"entity": {"id": payment_id, "order_id": order_id}}}
    if refund_id:
        payload["refund"] = {"entity": {"id": refund_id, "payment_id": payment_id}}
    raw = json.dumps({"event": event, "payload": payload}).encode()
    return raw, {"X-Razorpay-Signature": _sig(WH_SECRET, raw), "X-Razorpay-Event-Id": event_id}


async def test_config_reports_provider(api, monkeypatch):
    assert (await api.get("/api/payments/config")).json() == {"provider": "simulated", "key_id": None}
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_key")
    monkeypatch.setattr(settings, "razorpay_key_secret", "s")
    assert (await api.get("/api/payments/config")).json() == {"provider": "razorpay", "key_id": "rzp_test_key"}


async def test_endpoints_404_when_razorpay_is_off(api):
    h = await signup(api, "off")
    order = await buy_to_order(api, h)
    assert (await api.post("/api/payments/razorpay/order", json={"order_id": order["id"]}, headers=h)).status_code == 404
    assert (await api.post("/webhooks/razorpay", content=b"{}")).status_code == 401  # no secret: refuse all


async def test_create_order_uses_server_amount_and_is_reusable(api, razorpay_on):
    h, order, data = await _start(api)
    assert razorpay_on == [{"amount": order["amount_cents"], "receipt": order["id"]}]
    assert data["amount"] == order["amount_cents"] and data["currency"] == "INR"
    assert data["key_id"] == "rzp_test_key" and data["razorpay_order_id"] == "order_fake1"
    again = await api.post("/api/payments/razorpay/order", json={"order_id": order["id"]}, headers=h)
    assert again.json()["razorpay_order_id"] == "order_fake1" and len(razorpay_on) == 1  # no second Razorpay order


async def test_cannot_pay_someone_elses_order(api, razorpay_on):
    _, order, _ = await _start(api, "owner")
    thief = await signup(api, "thief")
    assert (await api.post("/api/payments/razorpay/order", json={"order_id": order["id"]}, headers=thief)).status_code == 404


async def test_verify_marks_order_paid(api, razorpay_on):
    h, order, data = await _start(api)
    body = {"order_id": order["id"], "razorpay_order_id": data["razorpay_order_id"], "razorpay_payment_id": "pay_9",
            "razorpay_signature": _sig(KEY_SECRET, f"{data['razorpay_order_id']}|pay_9".encode())}
    r = await api.post("/api/payments/razorpay/verify", json=body, headers=h)
    assert r.status_code == 200 and r.json()["order_status"] == "paid"
    assert (await stock(api))["sold"] == 1
    # replaying it, or the webhook for the same payment arriving after, changes nothing
    assert (await api.post("/api/payments/razorpay/verify", json=body, headers=h)).json()["outcome"] == "duplicate_event"
    raw, hdr = _webhook("payment.captured", data["razorpay_order_id"], "pay_9", "evt_late")
    assert (await api.post("/webhooks/razorpay", content=raw, headers=hdr)).json()["outcome"] == "already_paid"
    assert (await stock(api))["sold"] == 1


async def test_verify_rejects_forged_signature(api, razorpay_on):
    h, order, data = await _start(api)
    body = {"order_id": order["id"], "razorpay_order_id": data["razorpay_order_id"], "razorpay_payment_id": "pay_9",
            "razorpay_signature": "0" * 64}
    assert (await api.post("/api/payments/razorpay/verify", json=body, headers=h)).status_code == 400
    assert (await stock(api))["sold"] == 0


async def test_verify_rejects_a_payment_for_a_different_razorpay_order(api, razorpay_on):
    h, order, data = await _start(api)
    body = {"order_id": order["id"], "razorpay_order_id": "order_other", "razorpay_payment_id": "pay_9",
            "razorpay_signature": _sig(KEY_SECRET, b"order_other|pay_9")}
    assert (await api.post("/api/payments/razorpay/verify", json=body, headers=h)).status_code == 400


async def test_webhook_alone_can_pay_then_refund(api, razorpay_on):
    h, order, data = await _start(api)
    rid = data["razorpay_order_id"]
    raw, hdr = _webhook("payment.captured", rid)
    assert (await api.post("/webhooks/razorpay", content=raw, headers=hdr)).json()["outcome"] == "paid"
    assert (await api.post("/webhooks/razorpay", content=raw, headers=hdr)).json()["outcome"] == "duplicate_event"
    assert (await api.get(f"/api/orders/{order['id']}", headers=h)).json()["status"] == "paid"

    raw, hdr = _webhook("refund.processed", rid, event_id="evt_ref", refund_id="rfnd_1")
    assert (await api.post("/webhooks/razorpay", content=raw, headers=hdr)).json()["outcome"] == "refunded"
    s = await stock(api)
    assert s["sold"] == 0 and s["available"] == s["total"]


async def test_webhook_bad_signature_and_unknown_events(api, razorpay_on):
    _, _, data = await _start(api)
    raw, hdr = _webhook("payment.captured", data["razorpay_order_id"])
    assert (await api.post("/webhooks/razorpay", content=raw, headers={**hdr, "X-Razorpay-Signature": "bad"})).status_code == 401
    raw, hdr = _webhook("payout.processed", data["razorpay_order_id"], event_id="evt_x")
    r = await api.post("/webhooks/razorpay", content=raw, headers=hdr)
    assert r.status_code == 200 and r.json()["outcome"] == "ignored_unknown_type"
    bad = b"not json"
    assert (await api.post("/webhooks/razorpay", content=bad, headers={"X-Razorpay-Signature": _sig(WH_SECRET, bad)})).status_code == 400


async def test_simulator_is_disabled_while_razorpay_is_on(api, razorpay_on):
    h, order, _ = await _start(api)
    assert (await api.post(f"/api/dev/payments/{order['id']}/simulate", headers=h)).status_code == 404