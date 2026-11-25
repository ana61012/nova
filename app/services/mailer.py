"""Email delivery for sign-in codes.

`Mailer` is the seam: anything with `async send_code(to, code, ttl_minutes)`.
Pick one with DROP_EMAIL_BACKEND = console | memory | smtp | brevo.
"""
import asyncio
import smtplib
from email.message import EmailMessage
from typing import Protocol

import httpx

from ..config import settings


class Mailer(Protocol):
    async def send_code(self, to: str, code: str, ttl_minutes: int) -> None: ...


def _body(code: str, ttl_minutes: int) -> str:
    return (
        f"Your nova sign-in code is {code}\n\n"
        f"It expires in {ttl_minutes} minutes and works once. "
        "If you didn't ask for it, you can ignore this email."
    )


class ConsoleMailer:
    """Local development: the 'email' is a line in the server log."""

    async def send_code(self, to: str, code: str, ttl_minutes: int) -> None:
        print(f"[mail] sign-in code for {to}: {code} (valid {ttl_minutes} min)", flush=True)


class MemoryMailer:
    """Tests and the load test: keeps the latest code per address."""

    def __init__(self) -> None:
        self.latest: dict[str, str] = {}
        self.count = 0

    async def send_code(self, to: str, code: str, ttl_minutes: int) -> None:
        self.latest[to] = code
        self.count += 1


class SMTPMailer:
    def _send_blocking(self, to: str, code: str, ttl_minutes: int) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, f"Your sign-in code: {code}"
        msg.set_content(_body(code, ttl_minutes))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)

    async def send_code(self, to: str, code: str, ttl_minutes: int) -> None:
        # smtplib blocks; keep it off the event loop.
        await asyncio.to_thread(self._send_blocking, to, code, ttl_minutes)


class BrevoMailer:
    """Sends over HTTPS (port 443) via Brevo's transactional API.

    Use this on hosts that block outbound SMTP (Render's free tier blocks 25/465/587).
    """

    URL = "https://api.brevo.com/v3/smtp/email"

    async def send_code(self, to: str, code: str, ttl_minutes: int) -> None:
        payload = {
            "sender": {"name": settings.brevo_sender_name, "email": settings.brevo_sender_email},
            "to": [{"email": to}],
            "subject": f"Your sign-in code: {code}",
            "textContent": _body(code, ttl_minutes),
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(self.URL, json=payload, headers={"api-key": settings.brevo_api_key})
        if r.status_code >= 300:
            # Surfaces in the Render log via the caller's "mail error" line.
            raise RuntimeError(f"brevo {r.status_code}: {r.text[:300]}")


def build_mailer() -> Mailer:
    backend = settings.email_backend.lower()
    if backend == "brevo":
        return BrevoMailer()
    if backend == "smtp":
        return SMTPMailer()
    if backend == "memory":
        return MemoryMailer()
    return ConsoleMailer()


# Looked up as `mailer.mailer` at call time, so tests can swap it.
mailer: Mailer = build_mailer()