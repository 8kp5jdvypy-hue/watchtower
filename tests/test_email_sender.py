from __future__ import annotations

import os

from tradebot.email_sender import DevEmailSender, ResendEmailSender, build_email_sender


def test_dev_email_sender_never_raises_and_makes_no_network_call():
    DevEmailSender().send_magic_link("alice@example.com", "https://app.perchmarkets.com/verify?token=abc")


def test_build_email_sender_defaults_to_dev_sender_without_credentials(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)
    assert isinstance(build_email_sender(), DevEmailSender)


def test_build_email_sender_uses_resend_when_credentials_are_present(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "login@perchmarkets.com")
    sender = build_email_sender()
    assert isinstance(sender, ResendEmailSender)


def test_resend_email_sender_posts_expected_payload(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("tradebot.email_sender.requests.post", fake_post)

    sender = ResendEmailSender(api_key="re_test_key", from_email="login@perchmarkets.com")
    sender.send_magic_link("alice@example.com", "https://app.perchmarkets.com/verify?token=abc")

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_test_key"
    assert captured["json"]["from"] == "login@perchmarkets.com"
    assert captured["json"]["to"] == ["alice@example.com"]
    assert "https://app.perchmarkets.com/verify?token=abc" in captured["json"]["html"]
