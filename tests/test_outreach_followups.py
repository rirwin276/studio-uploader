from __future__ import annotations

from datetime import datetime, timedelta, timezone

import outreach_followups


def test_followups_are_inert_without_smtp(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    monkeypatch.setattr(
        outreach_followups.outreach_tracking,
        "list_all",
        lambda _core: (_ for _ in ()).throw(AssertionError("should not scan")),
    )
    assert outreach_followups.process_due_followups(object()) == 0


def test_unauthorized_email_is_never_sent(monkeypatch):
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_USER", "sender@example.org")
    monkeypatch.setenv("SMTP_PASS", "test-password")
    monkeypatch.setattr(
        outreach_followups.outreach_tracking,
        "list_all",
        lambda _core: {
            "example-club": {
                "handle": "example-club",
                "storefront_name": "Example Club",
                "contact_email": "club@example.org",
                "followup_due_at": due,
                "email_authorized": False,
            }
        },
    )
    monkeypatch.setattr(
        outreach_followups.smtplib,
        "SMTP",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SMTP should not run")),
    )
    assert outreach_followups.process_due_followups(object()) == 0
