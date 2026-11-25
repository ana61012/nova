"""The Brevo mailer posts to the HTTPS API and raises on a failed send."""
import httpx
import pytest

from app.config import settings
from app.services import mailer as mailer_mod


def _patch_client(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        mailer_mod.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


async def test_brevo_sends_code_with_api_key(monkeypatch):
    monkeypatch.setattr(settings, "brevo_api_key", "xkeysib-test")
    monkeypatch.setattr(settings, "brevo_sender_email", "no-reply@example.com")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["api-key"]
        seen["body"] = request.read().decode()
        return httpx.Response(201, json={"messageId": "x"})

    _patch_client(monkeypatch, handler)
    await mailer_mod.BrevoMailer().send_code("ann@example.com", "123456", 10)
    assert seen["key"] == "xkeysib-test"
    assert "123456" in seen["body"] and "ann@example.com" in seen["body"]


async def test_brevo_raises_on_rejected_send(monkeypatch):
    _patch_client(monkeypatch, lambda r: httpx.Response(401, json={"message": "Key not found"}))
    with pytest.raises(RuntimeError, match="brevo 401"):
        await mailer_mod.BrevoMailer().send_code("ann@example.com", "123456", 10)


def test_build_mailer_picks_brevo(monkeypatch):
    monkeypatch.setattr(settings, "email_backend", "brevo")
    assert isinstance(mailer_mod.build_mailer(), mailer_mod.BrevoMailer)