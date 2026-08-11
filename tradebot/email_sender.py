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

from tradebot.accounts import MAGIC_LINK_TTL_MINUTES

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

# Hosted on app.perchmarkets.com's own static assets (deployed alongside
# the dashboard in web-app/public/brand/) rather than data: URIs or the
# marketing site -- email clients generally block data: image sources
# outright, and this keeps the asset's lifecycle tied to the one Worker
# this code already assumes is live. Same mark as the website (see
# web-app/src/components/PerchMark.jsx) rendered once as a static PNG,
# not a new logo.
_MARK_URL = "https://app.perchmarkets.com/brand/perch-mark-email.png"

_PREHEADER = "Your secure link to enter Perch."

# Table-based, inline-styled, no @font-face -- most email clients don't
# load custom web fonts reliably (Outlook desktop doesn't at all), so
# this leans on a system sans-serif stack and lets color/spacing/type
# scale carry the brand instead, per the same "premium from restraint"
# language the rest of the product uses. color-scheme meta tags pin the
# dark palette so Gmail/Apple Mail dark-mode inversion doesn't invert it
# into something illegible.
_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="dark light">
<meta name="supported-color-schemes" content="dark light">
<title>Enter Perch</title>
</head>
<body style="margin:0;padding:0;background-color:#05070a;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;mso-hide:all;">__PREHEADER__&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#05070a" style="background-color:#05070a;">
<tr><td align="center" style="padding:40px 20px;">
<table role="presentation" width="480" cellpadding="0" cellspacing="0" style="width:480px;max-width:100%;">

<tr><td align="center" style="padding-bottom:28px;">
<img src="__MARK_URL__" width="36" height="36" alt="Perch" style="display:block;border:0;outline:none;" />
</td></tr>

<tr><td bgcolor="#0a0d12" style="background-color:#0a0d12;border:1px solid #1c222c;border-radius:10px;padding:40px 36px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">

<p style="margin:0 0 6px;font-size:11px;line-height:1;letter-spacing:0.16em;color:#34e2ff;font-weight:700;text-transform:uppercase;">PERCH</p>
<h1 style="margin:0 0 14px;font-size:22px;line-height:1.3;color:#eef2f6;font-weight:700;letter-spacing:-0.005em;">Welcome to Perch</h1>
<p style="margin:0 0 28px;font-size:15px;line-height:1.55;color:#8b95a3;">Your secure sign-in link is ready.</p>

<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 28px;">
<tr><td bgcolor="#34e2ff" style="background-color:#34e2ff;border-radius:6px;">
<a href="__LINK_URL__" target="_blank" style="display:inline-block;padding:14px 30px;font-size:15px;font-weight:700;letter-spacing:0.02em;color:#05070a;text-decoration:none;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Enter Perch</a>
</td></tr>
</table>

<p style="margin:0 0 8px;font-size:13px;line-height:1.6;color:#8b95a3;">This link securely signs you into your Perch account. It expires in __TTL_MINUTES__ minutes and can only be used once.</p>
<p style="margin:0 0 28px;font-size:13px;line-height:1.6;color:#737f94;">If you didn't request this email, you can safely ignore it — no account changes happen until the link above is used.</p>

<p style="margin:0 0 4px;font-size:12px;line-height:1.5;color:#737f94;">If the button doesn't work, copy and paste this link:</p>
<p style="margin:0;font-size:12px;line-height:1.5;word-break:break-all;"><a href="__LINK_URL__" style="color:#34e2ff;text-decoration:none;">__LINK_URL__</a></p>

</td></tr>

<tr><td align="center" style="padding-top:24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<p style="margin:0;font-size:12px;line-height:1.6;color:#737f94;">— Perch Markets</p>
</td></tr>

</table>
</td></tr>
</table>
</body>
</html>
"""

_TEXT_TEMPLATE = """\
PERCH

Welcome to Perch. Your secure sign-in link is ready.

Enter Perch: __LINK_URL__

This link securely signs you into your Perch account. It expires in \
__TTL_MINUTES__ minutes and can only be used once.

If you didn't request this email, you can safely ignore it — no account \
changes happen until the link above is used.

— Perch Markets
"""


def _render_magic_link_email(link_url: str) -> tuple[str, str]:
    """Returns (html, text). Pure string substitution, not str.format —
    the template is full of literal CSS braces."""
    html = (
        _HTML_TEMPLATE
        .replace("__PREHEADER__", _PREHEADER)
        .replace("__MARK_URL__", _MARK_URL)
        .replace("__LINK_URL__", link_url)
        .replace("__TTL_MINUTES__", str(MAGIC_LINK_TTL_MINUTES))
    )
    text = (
        _TEXT_TEMPLATE
        .replace("__LINK_URL__", link_url)
        .replace("__TTL_MINUTES__", str(MAGIC_LINK_TTL_MINUTES))
    )
    return html, text


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
        html, text = _render_magic_link_email(link_url)
        response = requests.post(
            RESEND_API_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "from": self._from_email,
                "to": [to_email],
                "subject": "Your Perch sign-in link",
                "html": html,
                "text": text,
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
