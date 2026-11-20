"""Email delivery for sign-in codes.

`Mailer` is the seam: anything with `async send_code(to, code, ttl_minutes)`.
Pick one with DROP_EMAIL_BACKEND = console | memory | smtp.
"""
import asyncio
import smtplib
from email.message import EmailMessage
from typing import Protocol

from ..config import settings


class Mailer(Protocol):
    async def send_code(self, to: str, code: str, ttl_minutes: int) -> None: ...


def _body(code: str, ttl_minutes: int) -> str:
    return (
        f"Your DropEngine sign-in code is {code}\n\n"
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


def build_mailer() -> Mailer:
    backend = settings.email_backend.lower()
    if backend == "smtp":
        return SMTPMailer()
    if backend == "memory":
        return MemoryMailer()
    return ConsoleMailer()


# Looked up as `mailer.mailer` at call time, so tests can swap it.
mailer: Mailer = build_mailer()