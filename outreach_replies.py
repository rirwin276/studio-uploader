"""Inbound replies from prospects.

An outreach email that nobody reads the answer to is a mailshot. This receives
the replies, files each one against the store it is about, and decides how
little to do with it.

Deliberately timid. The only automatic action is deleting a store when someone
plainly asks to be left alone, because that is the one request it would be rude
to make them repeat. Everything else — a question, a maybe, anything the wording
does not settle — is left for a person, and every reply is forwarded either way
so nothing is only ever seen by a classifier.

Inert until OUTREACH_INBOUND_SECRET is set.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
from email.utils import parseaddr
from typing import Any, Dict, List, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse

import outreach_mail
import outreach_tracking
from outreach_auth import require_outreach_secret


_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


_INSTALL_LOCK = threading.Lock()
_INSTALLED_APP_IDS: set[int] = set()
_MAX_REPLIES_KEPT = 20
_MAX_BODY = 8000

# Read as "leave us alone", with no room for a second meaning. Anything softer
# than these is a conversation, not an instruction.
_OPT_OUT_PHRASES = (
    "no thanks",
    "no thank you",
    "not interested",
    "please remove",
    "remove us",
    "remove me",
    "unsubscribe",
    "take us off",
    "take me off",
    "opt out",
    "stop emailing",
    "do not contact",
    "don't contact",
    "delete it",
    "delete the store",
    "take it down",
)

# A question mark, or any of these, means somebody wants an answer — and an
# answer is not something to automate.
_QUESTION_MARKERS = (
    "?",
    "how much",
    "how does",
    "who are you",
    "is this real",
    "what is this",
    "can we",
    "could we",
    "would we",
    "call me",
    "tell me more",
)


def configured() -> bool:
    return bool(os.getenv("OUTREACH_INBOUND_SECRET", "").strip())


def _authorized(request: Request) -> bool:
    expected = os.getenv("OUTREACH_INBOUND_SECRET", "").strip()
    if not expected:
        return False
    supplied = (
        request.headers.get("X-Outreach-Inbound-Secret", "")
        or request.query_params.get("secret", "")
    ).strip()
    return bool(supplied) and secrets.compare_digest(supplied, expected)


def _first(payload: Dict[str, Any], *paths: str) -> str:
    """Pull a field without betting on one provider's key names.

    Inbound webhook shapes differ between providers and between versions of the
    same provider, and a reply lost to a renamed key is a reply nobody answers.
    """
    for path in paths:
        current: Any = payload
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                current = None
                break
        if isinstance(current, list) and current:
            current = current[0]
        if isinstance(current, dict):
            current = current.get("address") or current.get("email") or ""
        if isinstance(current, str) and current.strip():
            return current.strip()
    return ""


def parse_inbound(payload: Dict[str, Any]) -> Dict[str, str]:
    body = _first(payload, "text", "data.text", "plain", "data.plain", "body", "data.body")
    if not body:
        html = _first(payload, "html", "data.html")
        # A plain-text part is preferred, but a reply that only arrived as HTML
        # still has to be readable rather than discarded.
        body = re.sub(r"<[^>]+>", " ", html) if html else ""
    return {
        "from": _first(payload, "from", "data.from", "sender", "data.sender", "envelope.from"),
        "to": _first(payload, "to", "data.to", "recipient", "data.recipient", "envelope.to"),
        "subject": _first(payload, "subject", "data.subject"),
        "body": re.sub(r"\s+", " ", body).strip()[:_MAX_BODY],
    }


def _quoted_trimmed(body: str) -> str:
    """Everything before the quoted original.

    Our own email says "reply no thanks and I'll delete it". Left in place that
    sentence is quoted back in every reply, and a classifier reading the whole
    thread would find an opt-out in a message asking a friendly question.
    """
    cut = re.split(
        r"(?:^|\s)(?:on .{0,80}wrote:|-{2,}\s*original message|from:\s)",
        body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return cut.strip() or body.strip()


def classify(body: str) -> str:
    """opt_out, question, or unclear. Never "interested" — that is a judgement."""
    text = _quoted_trimmed(body).lower()
    if not text:
        return "unclear"
    if any(marker in text for marker in _QUESTION_MARKERS):
        # A question wins over an opt-out phrase. "Not interested unless you can
        # do youth sizes — can you?" is a question, and treating it as a refusal
        # would delete a store somebody was asking to keep.
        return "question"
    if any(phrase in text for phrase in _OPT_OUT_PHRASES):
        return "opt_out"
    return "unclear"


def _match_store(core: Any, sender: str, recipient: str, subject: str) -> str:
    """Which store this reply is about.

    The sender address is the reliable signal: it is the address we wrote to.
    The subject is a fallback for a reply sent from a colleague's mailbox.
    """
    _name, address = parseaddr(sender or "")
    address = address.strip().lower()
    try:
        rows = outreach_tracking.list_all(core)
    except Exception:
        return ""

    if address:
        for handle, state in rows.items():
            if str(state.get("contact_email") or "").strip().lower() == address:
                return handle
        domain = address.rsplit("@", 1)[-1]
        for handle, state in rows.items():
            known = str(state.get("contact_email") or "").strip().lower()
            if known.endswith("@" + domain):
                return handle

    haystack = f"{subject} {recipient}".lower()
    for handle, state in rows.items():
        name = str(state.get("storefront_name") or "").strip().lower()
        if handle and handle in haystack:
            return handle
        if name and len(name) > 6 and name in haystack:
            return handle
    return ""


def _record(core: Any, handle: str, reply: Dict[str, str], decision: str) -> None:
    state = outreach_tracking.read(core, handle) or {}
    replies = state.get("replies") if isinstance(state.get("replies"), list) else []
    replies.append({
        "at": outreach_tracking.utc_iso(),
        "from": reply.get("from", "")[:200],
        "subject": reply.get("subject", "")[:200],
        "body": reply.get("body", "")[:2000],
        "decision": decision,
        "handled": decision == "opt_out",
    })
    patch: Dict[str, Any] = {
        "replies": replies[-_MAX_REPLIES_KEPT:],
        "last_reply_at": outreach_tracking.utc_iso(),
        "needs_human_reply": decision != "opt_out",
    }
    if decision == "opt_out":
        # A follow-up to somebody who just asked to be left alone is the worst
        # possible next email, so the clocks stop before anything else happens.
        patch.update({
            "followup_due_at": None,
            "delete_due_at": None,
            "email_authorized": False,
            "opted_out_at": outreach_tracking.utc_iso(),
        })
    outreach_tracking.update(core, handle, patch)


def _forward(reply: Dict[str, str], handle: str, decision: str) -> None:
    """Send it on to a human. Never the only copy — the ledger has it too."""
    to = os.getenv("OUTREACH_REPLY_TO", "").strip()
    if not to or not outreach_mail.configured():
        return
    label = {"opt_out": "opt-out, store deleted", "question": "needs your reply"}.get(
        decision, "unclear — needs your reply"
    )
    body = (
        f"Reply about {handle or 'an unmatched store'} ({label})\n\n"
        f"From: {reply.get('from', '')}\n"
        f"Subject: {reply.get('subject', '')}\n\n"
        f"{reply.get('body', '')}\n"
    )
    message = outreach_mail._message(
        to=to,
        subject=f"[outreach reply] {reply.get('subject') or handle or 'no subject'}",
        body=body,
    )
    try:
        outreach_mail.send(message)
    except Exception as exc:
        print(f"[outreach-replies] could not forward reply for {handle}: {exc}")


def handle_reply(core: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    reply = parse_inbound(payload)
    decision = classify(reply["body"])
    handle = _match_store(core, reply["from"], reply["to"], reply["subject"])

    deleted = False
    if handle:
        _record(core, handle, reply, decision)
        if decision == "opt_out":
            deleted = _delete(core, handle, reply)

    # Forwarded whatever happened, including when no store matched: an
    # unmatched reply is still somebody writing to us.
    _forward(reply, handle, decision)
    return {
        "ok": True,
        "handle": handle,
        "decision": decision,
        "store_deleted": deleted,
        "matched": bool(handle),
    }


def _delete(core: Any, handle: str, reply: Dict[str, str]) -> bool:
    import uuid
    import time

    try:
        outreach_tracking.update(core, handle, {
            "status": "declined",
            "declined_at": outreach_tracking.utc_iso(),
            "review_decision": "opted_out",
            "review_note": ("Reply: " + reply.get("body", ""))[:300],
        })
        job_id = str(uuid.uuid4())
        core._job_set(job_id, status="queued", handle=handle, created_at=time.time())
        threading.Thread(
            target=core._run_shopify_deprovision_job,
            args=(job_id, handle),
            name=f"outreach-optout-{handle}",
            daemon=True,
        ).start()
        return True
    except Exception as exc:
        print(f"[outreach-replies] could not delete {handle} after opt-out: {exc}")
        return False


def pending_replies(core: Any) -> List[Dict[str, Any]]:
    """Replies a person still has to answer."""
    rows: List[Dict[str, Any]] = []
    try:
        ledger = outreach_tracking.list_all(core)
    except Exception:
        return rows
    for handle, state in ledger.items():
        if not outreach_tracking.is_outreach_source(state.get("source")):
            continue
        for reply in state.get("replies") or []:
            if reply.get("handled"):
                continue
            rows.append({
                "handle": handle,
                "organization": outreach_mail.organization_name(state),
                "at": reply.get("at"),
                "from": reply.get("from"),
                "subject": reply.get("subject"),
                "body": reply.get("body"),
                "decision": reply.get("decision"),
            })
    rows.sort(key=lambda row: str(row.get("at") or ""), reverse=True)
    return rows


def mark_answered(core: Any, handle: str) -> Dict[str, Any]:
    """Clear a store's replies off the queue once a person has answered them.

    Answering happens in a mail client, which this never sees, so the queue can
    only empty when somebody says it is dealt with.
    """
    state = outreach_tracking.read(core, handle) or {}
    if not state:
        raise LookupError(f"no outreach record for {handle}")
    replies = [dict(reply) for reply in (state.get("replies") or [])]
    for reply in replies:
        reply["handled"] = True
    outreach_tracking.update(core, handle, {
        "replies": replies,
        "needs_human_reply": False,
        "reply_answered_at": outreach_tracking.utc_iso(),
    })
    return {"ok": True, "handle": handle, "cleared": len(replies)}


def install_outreach_reply_routes(app: Any, core: Any) -> bool:
    app_id = id(app)
    with _INSTALL_LOCK:
        if app_id in _INSTALLED_APP_IDS:
            return False
        _INSTALLED_APP_IDS.add(app_id)

    @app.post("/api/outreach/replies/inbound")
    async def inbound_reply(request: Request):
        if not _authorized(request):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)
        try:
            return handle_reply(core, payload)
        except Exception as exc:
            # Answering 200 would tell the provider to stop retrying a reply we
            # just dropped on the floor.
            print(f"[outreach-replies] inbound failed: {exc}")
            return JSONResponse({"error": "Could not process the reply"}, status_code=500)

    @app.get("/api/outreach/replies")
    def list_replies(request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        return {
            "ok": True,
            "inbound_configured": configured(),
            "pending": pending_replies(core),
        }

    @app.post("/api/outreach/replies/{handle}/answered")
    def replies_answered(handle: str, request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        normalized = (handle or "").strip().lower()
        if not _SAFE_HANDLE.fullmatch(normalized):
            return JSONResponse({"error": "invalid store handle"}, status_code=400)
        try:
            return mark_answered(core, normalized)
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    return True
