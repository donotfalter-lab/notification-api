"""Building and sending the email, through a deliberately unreliable "provider"."""

import asyncio
import random
from email.message import EmailMessage
from typing import Protocol

import aiosmtplib

from app.config import WorkerSettings


class SimulatedProviderError(Exception):
    """Raised on purpose (SMTP_FAIL_RATE) so retries get exercised."""


class Sender(Protocol):
    async def __call__(self, *, to: str, subject: str, text: str) -> None: ...


def first_name(full_name: str) -> str:
    """First word of the customer's name; a friendly fallback if it's blank."""
    parts = full_name.split()
    return parts[0] if parts else "there"


def render_body(customer_name: str, body: str) -> str:
    return f"Hi {first_name(customer_name)},\n\n{body}\n"


class SmtpSender:
    """Sends via SMTP (Mailpit locally), with fake provider latency and failures."""

    def __init__(self, settings: WorkerSettings, rng: random.Random | None = None):
        self.settings = settings
        self.rng = rng or random.Random()

    async def __call__(self, *, to: str, subject: str, text: str) -> None:
        s = self.settings
        if s.smtp_delay_ms:
            await asyncio.sleep(self.rng.uniform(0, s.smtp_delay_ms) / 1000)
        if self.rng.random() < s.smtp_fail_rate:
            raise SimulatedProviderError("simulated provider failure (SMTP_FAIL_RATE)")

        message = EmailMessage()
        message["From"] = s.smtp_from
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)

        await aiosmtplib.send(
            message, hostname=s.smtp_host, port=s.smtp_port, timeout=s.smtp_timeout_seconds
        )
