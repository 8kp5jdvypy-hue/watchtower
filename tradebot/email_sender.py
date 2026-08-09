"""Sends the magic-link login email (see tradebot.accounts). Not named
`email.py` to avoid shadowing the stdlib `email` package.

Same seam pattern as tradebot.entitlements.BillingProvider: one
interface, one real implementation (Resend), one no-op implementation
for tests/dev so nothing here ever makes a network call outside
production.
"""
from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


class EmailSender:
    def send_magic_link(self, to_email: str, link_url: str) -> None:
        raise NotImplementedError


class DevEmailSender(EmailSender):
    """Logs the link instead of emailing it — used whenever RESEND_API_KEY
    isn't set (local dev, tests, replay)."""

    def send_magic_link(self, to_email: str, link_url: str) -> None:
        logger.info("magic link for %s (DevEmailSender, not actually sent): %s", to_email, link_url)


class ResendEmailSender(EmailSender):
    """See https://resend.com/docs/api-reference/emails/send-email.
    `from_email` must be on a domain verified in the Resend dashboard —
    that verification (DNS records on perchmarkets.com) is a one-time
    manual step, not something this code can do."""

    def __init__(self, api_key: str, from_email: str) -> None:
        self._api_key = api_key
        self._from_email = from_email

    def send_magic_link(self, to_email: str, link_url: str) -> None:
        response = requests.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "from": self._from_email,
                "to": [to_email],
                "subject": "Your Perch sign-in link",
                "html": (
                    f'<p>Click to sign in to Perch:</p><p><a href="{link_url}">{link_url}</a></p>'
                    "<p>This link expires in 15 minutes and can only be used once. "
                    "If you didn't request this, you can ignore it.</p>"
                ),
            },
            timeout=10,
        )
        response.raise_for_status()


def build_email_sender() -> EmailSender:
    """Reads RESEND_API_KEY / RESEND_FROM_EMAIL from the environment —
    falls back to DevEmailSender if either is missing, same fail-open-to-
    log-only discipline as tradebot.alerts.ConsoleAlerter."""
    api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("RESEND_FROM_EMAIL")
    if not api_key or not from_email:
        return DevEmailSender()
    return ResendEmailSender(api_key=api_key, from_email=from_email)
