"""Nightly prospect discovery.

The research step used to live in a chat window, which meant a person had to be
there. This runs it as a job: ask a model to find organizations that fit the
brief, screen what comes back, and hand the survivors to the same intake queue
a human would have used.

Nothing here emails anyone. Discovery fills the review queue; a person still
accepts or declines every store before a stranger hears from us.

Inert until OPENAI_API_KEY is set, so it can ship and deploy before anyone has
decided to spend money on it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Tuple

import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import outreach_logo
import outreach_tracking
import outreach_verify
from outreach_auth import require_outreach_secret


# Run history lives in the same ledger as the stores, under a handle no real
# store can take, so there is one place to look and nothing new to provision.
RUN_LEDGER_HANDLE = "zz-discovery-runs"
_MAX_RUNS_KEPT = 25

_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_INSTALL_LOCK = threading.Lock()
_INSTALLED_APP_IDS: set[int] = set()

# One run at a time. The request used to be allowed to wait five minutes and
# retry three times, which made a perfectly ordinary provider slowdown look
# exactly like a dead worker. Discovery is deliberately bounded below, so an
# eight-minute lease is generous and a stale lease cannot freeze the dashboard
# for most of an hour.
_STALE_RUN_SECONDS = 8 * 60
_RUN_STATE_LOCK = threading.Lock()
_RUN_STARTED_AT: float | None = None
_RUN_TOKEN = ""
_RUN_PHASE = ""
_RUN_UPDATED_AT: float | None = None


def _current_run_token() -> str:
    with _RUN_STATE_LOCK:
        return _RUN_TOKEN


def _owns_run(token: str) -> bool:
    with _RUN_STATE_LOCK:
        return bool(token) and token == _RUN_TOKEN


def _set_run_phase(token: str, phase: str) -> None:
    """Publish a small, safe progress snapshot for the review screen.

    The token matters when a worker is killed or a lease is taken over: an old
    worker must never overwrite the status of the replacement that owns it.
    """
    global _RUN_PHASE, _RUN_UPDATED_AT
    with _RUN_STATE_LOCK:
        if token and token == _RUN_TOKEN:
            _RUN_PHASE = str(phase or "")[:180]
            _RUN_UPDATED_AT = time.time()


def _run_snapshot() -> Dict[str, Any]:
    now = time.time()
    with _RUN_STATE_LOCK:
        started = _RUN_STARTED_AT
        age = max(0.0, now - started) if started is not None else 0.0
        stale_age = max(0.0, now - (_RUN_UPDATED_AT or started or now))
        return {
            "running": started is not None,
            "age_seconds": round(age, 1) if started is not None else None,
            "phase": _RUN_PHASE or None,
            "updated_at_epoch": _RUN_UPDATED_AT,
            "stale_after_seconds": _STALE_RUN_SECONDS,
            "stale_for_seconds": round(stale_age, 1) if started is not None else None,
            "can_clear": started is not None and stale_age >= _STALE_RUN_SECONDS,
        }


def _begin_run() -> Tuple[bool, float]:
    """Claim the right to run. Returns (allowed, age of the run in the way)."""
    global _RUN_STARTED_AT, _RUN_TOKEN, _RUN_PHASE, _RUN_UPDATED_AT
    now = time.time()
    with _RUN_STATE_LOCK:
        started = _RUN_STARTED_AT
        if started is not None:
            age = now - started
            stale_age = now - (_RUN_UPDATED_AT or started)
            if stale_age < _STALE_RUN_SECONDS:
                return False, age
            print(
                f"[outreach-discovery] taking over from a run with no progress for "
                f"{stale_age / 60:.0f} minutes — treating it as dead"
            )
        _RUN_STARTED_AT = now
        _RUN_TOKEN = uuid.uuid4().hex
        _RUN_PHASE = "Starting discovery"
        _RUN_UPDATED_AT = now
        return True, 0.0


def _end_run(token: str = "") -> None:
    """Release a run only when its owner still has the lease.

    Without the token, a pre-timeout worker that eventually wakes up could
    clear the state for the newer worker that took over its stale lease.
    """
    global _RUN_STARTED_AT, _RUN_TOKEN, _RUN_PHASE, _RUN_UPDATED_AT
    with _RUN_STATE_LOCK:
        if token and token != _RUN_TOKEN:
            return
        _RUN_STARTED_AT = None
        _RUN_TOKEN = ""
        _RUN_PHASE = ""
        _RUN_UPDATED_AT = time.time()


def clear_stuck_run() -> bool:
    """Forget the run in progress. Returns whether there was one."""
    global _RUN_STARTED_AT, _RUN_TOKEN, _RUN_PHASE, _RUN_UPDATED_AT
    with _RUN_STATE_LOCK:
        had = _RUN_STARTED_AT is not None
        _RUN_STARTED_AT = None
        _RUN_TOKEN = ""
        _RUN_PHASE = ""
        _RUN_UPDATED_AT = time.time()
    return had

DEFAULT_LIMIT = 5
MAX_LIMIT = 10
# Ask for a small reserve. A candidate can be real and still fail one of the
# deterministic checks (duplicate, bad direct-logo URL, dead contact domain).
# Returning exactly five candidates to fill five slots made one normal miss
# look like the model "couldn't find anyone".
MAX_RESEARCH_CANDIDATES = 12
DEFAULT_MAX_SEARCH_ROUNDS = 6
MAX_SEARCH_ROUNDS = 10

# Left to its own devices the model finds one easy vein and stays in it — two
# runs in a row of nothing but volunteer fire departments. The brief listing
# every category is not enough, because listing is not the same as steering.
# Each run is pointed at a slice of this list instead, so variety comes from
# the schedule rather than from hoping.
CATEGORY_DEFINITIONS = {
    "youth_team_sports": (
        "one independent youth soccer, baseball, softball, basketball, flag-football "
        "or lacrosse team/club — not a league, school or multi-team academy"
    ),
    "rowing_and_paddling": (
        "small rowing, crew, sailing, canoe, kayak or dragon-boat clubs"
    ),
    "aquatics": (
        "small independent swim, dive, water-polo or masters-swim clubs"
    ),
    "combat_sports": (
        "small independent martial-arts, judo, wrestling, boxing or fencing clubs"
    ),
    "outdoor_competition": (
        "small archery, disc-golf, climbing or orienteering clubs"
    ),
    "endurance_sports": (
        "small local cycling, running, trail-running or triathlon clubs"
    ),
    "adult_amateur_team_sports": (
        "small adult amateur soccer, rugby, softball, hockey or roller-derby clubs"
    ),
    "racquet_sports": (
        "small local pickleball, tennis, badminton or table-tennis clubs"
    ),
    "equestrian_and_rodeo": (
        "small independent equestrian, rodeo or riding competition clubs"
    ),
    "precision_and_league_sports": (
        "small local bowling, darts, cornhole or amateur-golf competition clubs"
    ),
}
CATEGORY_ROTATION = list(CATEGORY_DEFINITIONS)
_CATEGORIES_PER_RUN = 5
_RECENT_CATEGORY_RUNS = 3

_API_URL = "https://api.openai.com/v1/responses"

# Both are environment-driven on purpose. Model names and the exact web-search
# tool identifier change on the provider's schedule, not ours, and a rename
# should be a variable edit rather than a deploy.
# Discovery is a short, structured web lookup—not a long research task. The
# fast non-reasoning model keeps a single search from consuming minutes of
# background time or a high-reasoning-model budget. A stronger model remains a
# per-run override for a deliberately reviewed experiment.
DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_SEARCH_TOOL = "web_search"


CANDIDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "category",
                    "storefront_name",
                    "storefront_handle",
                    "type_of_store",
                    "primary_color",
                    "contact_email",
                    "organization_url",
                    "contact_source_url",
                    "logo_source_url",
                    "estimated_members",
                    "fit_evidence",
                    "independent_local_group",
                    "active_team_or_club",
                    "commercial_business",
                    "existing_merchandise",
                    "merchandise_check",
                    "why_it_qualifies",
                ],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": list(CATEGORY_DEFINITIONS),
                    },
                    "storefront_name": {"type": "string"},
                    "storefront_handle": {"type": "string"},
                    "type_of_store": {"type": "string"},
                    "primary_color": {"type": "string"},
                    "contact_email": {"type": "string"},
                    "organization_url": {"type": "string"},
                    "contact_source_url": {"type": "string"},
                    "logo_source_url": {"type": "string"},
                    "estimated_members": {"type": "integer"},
                    "fit_evidence": {"type": "string"},
                    "independent_local_group": {"type": "boolean"},
                    "active_team_or_club": {"type": "boolean"},
                    "commercial_business": {"type": "boolean"},
                    "existing_merchandise": {"type": "boolean"},
                    "merchandise_check": {"type": "string"},
                    "why_it_qualifies": {"type": "string"},
                },
            },
        }
    },
}


def _brief(
    limit: int,
    avoid: List[str],
    *,
    focus: List[str] | None = None,
    avoid_domains: List[str] | None = None,
    avoid_categories: List[str] | None = None,
    reviewer_feedback: List[str] | None = None,
) -> str:
    avoid_line = ""
    if avoid:
        shown = ", ".join(sorted(avoid)[:120])
        avoid_line += (
            "\n\nAlready contacted or already built — do not return any of these, "
            f"and do not return near-duplicates of them:\n{shown}"
        )
    if avoid_domains:
        shown = ", ".join(sorted(avoid_domains)[:120])
        avoid_line += (
            "\n\nWebsites already used. An organization at any of these domains is "
            f"already in the system, whatever name it goes by:\n{shown}"
        )
    if avoid_categories:
        shown = "; ".join(avoid_categories)
        avoid_line += (
            "\n\nCovered in the last few runs — skip these this time so the "
            f"outreach does not become one kind of organization:\n{shown}"
        )
    focus_line = ""
    if focus:
        shown = "\n".join(
            f"- {item}: {CATEGORY_DEFINITIONS.get(item, item)}" for item in focus
        )
        focus_line = (
            "\n\nFocus this run on these kinds of organization:\n" + shown +
            "\n\nReturn at most one organization per category in this response, and "
            "vary the state or region between them."
        )
    feedback_line = ""
    if reviewer_feedback:
        shown = "\n".join("- " + item for item in reviewer_feedback[:8])
        feedback_line = (
            "\n\nRecent reviewer rejections. Treat these as recurring failure patterns, "
            "not merely as organizations to avoid:\n" + shown
        )
    return f"""Find {limit} fully qualified micro teams or clubs that would plausibly want spirit wear and do not currently sell it online.

Who qualifies:
- A genuinely small, independent local team or competition club with its own identity and approximately 10–150 active members. Smaller is better.
- It has recurring practices, games, races, meets, tournaments or competitions. It is not merely a broad social-interest organization.
- It is normally volunteer-run, coach-led or managed by a very small local staff, and likely lacks the time or budget to set up a merchandise program.
- They have a public website with a visible logo.
- They have a contact email address published on their own website.
- They do NOT already sell apparel online. Inspect the home page, navigation and linked pages for Store, Shop, Merch, Apparel, Spirit Wear, Team Store or Gear. Follow obvious external shop links too. If any merchandise exists, skip them.
- Prefer United States organizations.

Skip immediately:
- Anything with an existing online store, even a bad one.
- Commercial dance studios, dance academies and multi-location gymnastics/cheer businesses.
- Leagues, multi-team academies, multi-location businesses, established programs, schools, school districts, PTOs, booster clubs tied to a school, churches, Scout troops, military posts, municipal/county departments, universities, large nonprofits, franchises, national brands and pro teams.
- Organizations that are a program inside a larger institution rather than an independent local group with its own identity.
- Organizations that appear to have more than 150 active members. Do not return 150 as a placeholder when the size is unknown.
- Anyone whose only contact is a web form with no email address.
- Anyone whose logo you cannot find as a direct image file on their own site.
- Anyone you are not confident is a real, currently active organization.

Hard rules:
- Only use information publicly visible on the organization's own website.
- category must be one of the supplied category IDs and must accurately describe the group.
- estimated_members must be a good-faith estimate supported by fit_evidence from the official site. If there is no reasonable evidence that the group is under 150, skip it.
- independent_local_group and active_team_or_club must both be true. commercial_business and existing_merchandise must both be false.
- merchandise_check must say which official navigation/page and external links you checked. Never claim "no store" from search-result text alone.
- Never guess an email address or construct one from a pattern. If you cannot see a real published address, skip the organization.
- logo_source_url should be the exact direct image URL copied from the official site's HTML or image link. Prefer an SVG or the largest official image available. Never invent a conventional path such as /logo.png, /images/logo.png, or /assets/logo.svg. Logos are never AI-redrawn, so skip the group if only a tiny or unclear copy exists.
- storefront_handle: lowercase letters, numbers and hyphens only. "St. Mary's Rowing" becomes st-marys-rowing.
- storefront_name: the organization name followed by " Team Store".
- primary_color: one common color name from their branding (Navy, Red, Royal Blue, Forest Green, Maroon, Black, Charcoal, Purple, Orange, Gold).
- why_it_qualifies: one sentence, including where you checked for an existing store and what you found.

Return all {limit} candidates. Do not stop after the first two or three that
look promising. Never fill the list by guessing a contact, size, logo or store
status; the caller will run another search round for any unfilled slots.
{focus_line}{avoid_line}{feedback_line}"""


def configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def enabled() -> bool:
    """Whether the nightly run is allowed to fire. On-demand runs ignore this."""
    return os.getenv("OUTREACH_DISCOVERY_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }


def nightly_limit() -> int:
    try:
        value = int(os.getenv("OUTREACH_DISCOVERY_LIMIT", str(DEFAULT_LIMIT)))
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _research_limit(requested: int) -> int:
    """Return enough candidates to survive ordinary deterministic rejects.

    This is intentionally capped. More web searching is not a substitute for a
    better brief, and an overnight run should have a predictable ceiling.
    """
    return min(MAX_RESEARCH_CANDIDATES, max(requested + 2, requested * 2))


def max_search_rounds() -> int:
    """A high but finite emergency ceiling for a single discovery job.

    Discovery keeps refilling rejected slots. The ceiling exists only to stop
    a broken provider or an impossibly narrow public-web night from spending
    forever; a shortfall is reported explicitly rather than disguised as a
    successful smaller batch.
    """
    try:
        value = int(os.getenv(
            "OUTREACH_DISCOVERY_MAX_ROUNDS", str(DEFAULT_MAX_SEARCH_ROUNDS)
        ))
    except (TypeError, ValueError):
        value = DEFAULT_MAX_SEARCH_ROUNDS
    return max(2, min(MAX_SEARCH_ROUNDS, value))


def _timeouts() -> Tuple[float, float]:
    """Bound provider waiting so one stalled search cannot hold discovery.

    Environment overrides are useful for temporary provider incidents, but a
    mistaken huge value must not reintroduce the old all-night freeze.
    """
    def number(name: str, fallback: float, minimum: float, maximum: float) -> float:
        try:
            value = float(os.getenv(name, str(fallback)))
        except (TypeError, ValueError):
            value = fallback
        return min(maximum, max(minimum, value))

    return (
        number("OUTREACH_DISCOVERY_CONNECT_TIMEOUT_SECONDS", _CONNECT_TIMEOUT_SECONDS, 5, 30),
        number("OUTREACH_DISCOVERY_READ_TIMEOUT_SECONDS", _READ_TIMEOUT_SECONDS, 30, 180),
    )


def _ledger(core: Any) -> Dict[str, Dict[str, Any]]:
    try:
        rows = outreach_tracking.list_all(core)
    except Exception:
        return {}
    return {h: v for h, v in rows.items() if h != RUN_LEDGER_HANDLE}


def _known_handles(core: Any) -> List[str]:
    return list(_ledger(core).keys())


def _domain(url: Any) -> str:
    """The registrable-ish host of a URL, lowercased, without www."""
    text = str(url or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    host = text.split("/", 1)[0].split("?", 1)[0].split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _known_domains(core: Any) -> set[str]:
    """Websites already in the system.

    Handles are a weak identity for an organization — the same fire department
    is cassie-vfd one night and cassie-volunteer-fire-department the next, and
    both would pass a handle check. The website does not move.
    """
    domains = set()
    for state in _ledger(core).values():
        host = _domain(state.get("organization_url"))
        if host:
            domains.add(host)
    return domains


_FEEDBACK_LABELS = {
    "existing_merch": "already sold merchandise",
    "too_large": "was too large or established",
    "bad_logo": "had an unusable, inaccurate or redrawn logo",
    "not_team": "was not a real active team or competition club",
    "wrong_contact": "did not have the right published decision-maker contact",
    "duplicate": "was a duplicate",
    "other": "did not fit for another reason",
}


def _legacy_feedback_code(note: Any) -> str:
    """Give old free-form notes the same value as the new reason buttons."""
    text = str(note or "").strip().lower()
    if any(word in text for word in ("merch", "shop", "store already", "already sell")):
        return "existing_merch"
    if any(word in text for word in ("too big", "too large", "established", "large program")):
        return "too_large"
    if any(word in text for word in ("logo", "artwork", "redraw", "image")):
        return "bad_logo"
    if any(word in text for word in ("not a team", "not a club", "wrong audience")):
        return "not_team"
    if any(word in text for word in ("email", "contact")):
        return "wrong_contact"
    if "duplicate" in text:
        return "duplicate"
    return "other"


def _reviewer_feedback(core: Any) -> List[str]:
    """Turn review decisions into compact patterns the next search can use."""
    counts: Dict[str, int] = {}
    for state in _ledger(core).values():
        if str(state.get("review_decision") or "").lower() not in {"declined", "removed"}:
            continue
        code = str(state.get("review_reason_code") or "").strip().lower()
        if code not in _FEEDBACK_LABELS:
            code = _legacy_feedback_code(state.get("review_note"))
        counts[code] = counts.get(code, 0) + 1
    return [
        f"{count} recent candidate{'s' if count != 1 else ''} {_FEEDBACK_LABELS[code]}."
        for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _rotation_for_run(core: Any, *, offset: int = 0) -> Tuple[List[str], List[str]]:
    """Which categories to chase this run, and which to stay off.

    Stepping through the list by run count means variety is a property of the
    schedule rather than something the model has to be talked into.
    """
    history = recent_runs(core)
    cursor = len(history) * _CATEGORIES_PER_RUN + max(0, int(offset))
    focus = [
        CATEGORY_ROTATION[(cursor + offset) % len(CATEGORY_ROTATION)]
        for offset in range(min(_CATEGORIES_PER_RUN, len(CATEGORY_ROTATION)))
    ]
    recent: List[str] = []
    for run in history[:_RECENT_CATEGORY_RUNS]:
        for row in run.get("candidates") or []:
            kind = str(row.get("category") or row.get("type_of_store") or "").strip()
            if kind and kind.lower() not in {r.lower() for r in recent}:
                recent.append(kind)
    return focus, recent[:12]


# A token-per-minute ceiling is a budget, not a fault. It is shared across the
# organization, every model has one, and the response says how long to wait —
# so a run that hits it should wait rather than report failure and lose the
# search it already paid for.
_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 2
_MAX_RETRY_WAIT = 30.0
_CONNECT_TIMEOUT_SECONDS = 15
_READ_TIMEOUT_SECONDS = 120


def _retry_after(response: Any, attempt: int) -> float:
    """How long to wait, preferring what the provider asked for."""
    header = str(getattr(response, "headers", {}).get("retry-after") or "").strip()
    if header:
        try:
            return min(_MAX_RETRY_WAIT, max(1.0, float(header)))
        except ValueError:
            pass
    # The Responses API puts the wait in prose rather than a header:
    # "Please try again in 7.134s."
    match = re.search(r"try again in ([0-9.]+)s", str(getattr(response, "text", "")))
    if match:
        try:
            return min(_MAX_RETRY_WAIT, max(1.0, float(match.group(1)) + 1.0))
        except ValueError:
            pass
    return min(_MAX_RETRY_WAIT, 5.0 * (2 ** attempt))


def _ask_for_candidates(
    limit: int,
    avoid: List[str],
    *,
    focus: List[str] | None = None,
    avoid_domains: List[str] | None = None,
    avoid_categories: List[str] | None = None,
    reviewer_feedback: List[str] | None = None,
    model: str = "",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """One call out to the model. Returns (candidates, telemetry)."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    # A per-run override exists so a more capable model can be tried against the
    # same brief without a variable edit and a redeploy between comparisons.
    model = (model or os.getenv("OUTREACH_DISCOVERY_MODEL", DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    tool = os.getenv("OUTREACH_DISCOVERY_SEARCH_TOOL", DEFAULT_SEARCH_TOOL).strip() or DEFAULT_SEARCH_TOOL

    payload = {
        "model": model,
        "input": _brief(
            limit,
            avoid,
            focus=focus,
            avoid_domains=avoid_domains,
            avoid_categories=avoid_categories,
            reviewer_feedback=reviewer_feedback,
        ),
        "tools": [{"type": tool}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "outreach_candidates",
                "strict": True,
                "schema": CANDIDATE_SCHEMA,
            }
        },
    }
    started = time.time()
    connect_timeout, read_timeout = _timeouts()
    response = None
    last_request_error: requests.RequestException | None = None
    attempts = 0
    for attempt in range(_MAX_ATTEMPTS):
        attempts = attempt + 1
        try:
            response = requests.post(
                _API_URL,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=(connect_timeout, read_timeout),
            )
        except requests.RequestException as exc:
            last_request_error = exc
            if attempt == _MAX_ATTEMPTS - 1:
                raise RuntimeError(
                    f"OpenAI request timed out or could not connect after {attempts} attempts: {exc}"
                ) from exc
            time.sleep(_retry_after(None, attempt))
            continue
        if response.status_code < 300:
            break
        if response.status_code not in _RETRYABLE or attempt == _MAX_ATTEMPTS - 1:
            # A model rename, a bad key or a renamed tool is not going to fix
            # itself, so fail immediately and hand back the provider's own
            # message — it is the thing that says which of them it was.
            raise RuntimeError(
                f"OpenAI returned HTTP {response.status_code}: {response.text[:400]}"
            )
        time.sleep(_retry_after(response, attempt))
    if response is None:
        raise RuntimeError(
            f"OpenAI did not return a response: {last_request_error or 'unknown request failure'}"
        )
    body = response.json()

    text = ""
    for item in body.get("output") or []:
        for chunk in item.get("content") or []:
            if chunk.get("type") in {"output_text", "text"}:
                text += str(chunk.get("text") or "")
    if not text:
        text = str(body.get("output_text") or "")
    if not text.strip():
        raise RuntimeError("The model returned no candidate text")

    parsed = json.loads(text)
    candidates = parsed.get("candidates")
    if not isinstance(candidates, list):
        raise RuntimeError("The model did not return a candidate list")

    usage = body.get("usage") or {}
    telemetry = {
        "attempts": attempts,
        "focus": list(focus or []),
        "model": model,
        "search_tool": tool,
        "connect_timeout_seconds": connect_timeout,
        "read_timeout_seconds": read_timeout,
        "seconds": round(time.time() - started, 1),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }
    return candidates, telemetry


def _clean(
    candidate: Dict[str, Any],
    known: set[str],
    known_domains: set[str] | None = None,
    used_categories: set[str] | None = None,
) -> Tuple[Dict[str, Any] | None, str]:
    """Re-check the model's work before it can create anything.

    The brief already says all of this. That is not the same as it being true,
    and a bad handle or a page-instead-of-an-image logo is cheaper to reject
    here than to inspect in Shopify afterwards.
    """
    if not isinstance(candidate, dict):
        return None, "not an object"
    category = str(candidate.get("category") or "").strip().lower()
    if category not in CATEGORY_DEFINITIONS:
        return None, f"unknown or missing category {category!r}"
    if category in (used_categories or set()):
        return None, f"category {category} is already represented in this batch"
    identity = " ".join(
        str(candidate.get(field) or "").lower()
        for field in ("storefront_name", "type_of_store")
    )
    if any(term in identity for term in ("dance studio", "dance academy", "dance school")):
        return None, "commercial dance studios are outside the target audience"
    if candidate.get("independent_local_group") is not True:
        return None, "not confirmed as an independent local group"
    if candidate.get("active_team_or_club") is not True:
        return None, "not confirmed as an active team or competition club"
    if candidate.get("commercial_business") is not False:
        return None, "appears to be a commercial or established business"
    if candidate.get("existing_merchandise") is not False:
        return None, "model found existing merchandise"
    try:
        estimated_members = int(candidate.get("estimated_members"))
    except (TypeError, ValueError):
        return None, "member estimate is missing"
    if estimated_members < 10 or estimated_members > 150:
        return None, f"estimated size {estimated_members} is outside the 10–150 member target"
    fit_evidence = str(candidate.get("fit_evidence") or "").strip()
    merchandise_check = str(candidate.get("merchandise_check") or "").strip()
    if len(fit_evidence) < 12:
        return None, "no usable evidence that this is a small independent group"
    if len(merchandise_check) < 12:
        return None, "no usable evidence that the official site was checked for merchandise"
    handle = str(candidate.get("storefront_handle") or "").strip().lower()
    if not _SAFE_HANDLE.fullmatch(handle):
        return None, f"unusable handle {handle!r}"
    if handle in known:
        return None, "already known"

    urls = {}
    for field in ("organization_url", "contact_source_url", "logo_source_url"):
        value = str(candidate.get(field) or "").strip()
        if not value.lower().startswith("https://"):
            return None, f"{field} is not a public https URL"
        urls[field] = value

    # An address is only usable if we can see it on the organization's own
    # site. Prefer the model's address when it matches that evidence; otherwise
    # use the first published address and retain the real page as its source.
    published_emails, email_problem = outreach_verify.published_contact_emails(
        urls["organization_url"]
    )
    if not published_emails:
        return None, email_problem or "no usable contact email"
    proposed_email = str(candidate.get("contact_email") or "").strip().lower()
    email, contact_source = next(
        ((address, source) for address, source in published_emails if address == proposed_email),
        published_emails[0],
    )
    urls["contact_source_url"] = contact_source

    # Same organization, different handle. The website is the stable identity;
    # the name it gets called is not.
    host = _domain(urls["organization_url"])
    if host and host in (known_domains or set()):
        return None, f"{host} is already in the system"

    # The model is a researcher, not the final gate. Independently inspect the
    # official site's navigation for a shop and for clear evidence that this is
    # a large or multi-location program before spending anything on a build.
    has_merch, merch_problem = outreach_verify.existing_merchandise(
        urls["organization_url"]
    )
    if has_merch:
        return None, merch_problem or "official site links to an existing merchandise store"
    small_enough, size_problem = outreach_verify.small_group_fit(
        urls["organization_url"]
    )
    if not small_enough:
        return None, size_problem

    # Go and look. Everything above this line is the model's word, and a model
    # that invents an organization invents its website to match.
    verified, problem = outreach_verify.check_candidate({
        "contact_email": email,
        "organization_url": urls["organization_url"],
        "storefront_name": candidate.get("storefront_name"),
        "storefront_handle": handle,
    })
    if not verified:
        return None, problem

    # A model often gets the right organization but invents a plausible
    # `/logo.png`. Start with its proposed URL, then fall back to direct image
    # URLs actually embedded by the official homepage. This keeps the strict
    # image check without turning each mistaken model path into zero results.
    page_sources, page_problem = outreach_verify.logo_sources_on_organization_site(
        urls["organization_url"]
    )
    logo_candidates = [urls["logo_source_url"]] + [
        source for source in page_sources if source != urls["logo_source_url"]
    ]
    logo_problem = page_problem or "no verified official logo image"
    chosen_logo = ""
    for logo_url in logo_candidates:
        origin, origin_problem = outreach_logo.logo_origin(
            logo_url, urls["organization_url"]
        )
        if origin == "foreign":
            logo_problem = origin_problem
            continue
        logo_ok, problem = outreach_verify.logo_source_is_live(logo_url)
        if logo_ok:
            chosen_logo = logo_url
            break
        logo_problem = problem
    if not chosen_logo:
        return None, logo_problem
    urls["logo_source_url"] = chosen_logo

    return {
        "provider_request_id": f"discovery-{time.strftime('%Y%m%d')}-{handle}"[:120],
        "source_agent": "openai",
        "contact_email": email,
        "storefront_name": str(candidate.get("storefront_name") or "").strip()[:300],
        "storefront_handle": handle,
        "category": category,
        "type_of_store": str(candidate.get("type_of_store") or "").strip()[:120],
        "primary_color": str(candidate.get("primary_color") or "").strip()[:60],
        "screening_confirmed": True,
        "logo_source_reviewed": True,
        "email_authorized": False,
        "estimated_members": estimated_members,
        "fit_evidence": fit_evidence[:400],
        "merchandise_check": merchandise_check[:400],
        **urls,
    }, ""


def _submit(core: Any, payload: Dict[str, Any]) -> Tuple[bool, str]:
    from outreach_intake import queue_intake_payload

    try:
        result = queue_intake_payload(core, payload)
    except Exception as exc:
        return False, str(exc)[:200]
    if not isinstance(result, dict):
        # A rejection comes back as a response object, not a dict — a duplicate
        # handle or a URL that did not survive validation. Report it as a
        # skipped candidate rather than letting it read as a queued one.
        code = getattr(result, "status_code", 502)
        return False, f"rejected ({code})"
    status = str(result.get("status") or "")
    return status in {"intake_queued", "existing", "existing_store"}, status


def _record_run(core: Any, run: Dict[str, Any]) -> None:
    try:
        state = outreach_tracking.read(core, RUN_LEDGER_HANDLE) or {}
        runs = state.get("runs") if isinstance(state.get("runs"), list) else []
        runs.append(run)
        state["runs"] = runs[-_MAX_RUNS_KEPT:]
        state["handle"] = RUN_LEDGER_HANDLE
        state["last_run_at"] = run.get("finished_at")
        outreach_tracking.upsert(core, RUN_LEDGER_HANDLE, state)
    except Exception as exc:
        print(f"[discovery] could not record run: {exc}")


def recent_runs(core: Any) -> List[Dict[str, Any]]:
    try:
        state = outreach_tracking.read(core, RUN_LEDGER_HANDLE) or {}
        runs = state.get("runs")
        return list(reversed(runs))[:_MAX_RUNS_KEPT] if isinstance(runs, list) else []
    except Exception:
        return []


def run_discovery(
    core: Any,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    trigger: str = "manual",
    model: str = "",
    _reserved_token: str = "",
) -> Dict[str, Any]:
    """Find candidates and, unless this is a dry run, queue them for building.

    A dry run does everything except create anything: it is how you check what
    the brief actually returns before letting it loose on a real Shopify store.
    """
    if _reserved_token:
        token = _reserved_token
    else:
        allowed, age = _begin_run()
        if not allowed:
            raise RuntimeError(
                "A discovery run has been going for "
                f"{int(age // 60)}m {int(age % 60)}s. Wait for it, or clear it if it is stuck."
            )
        token = _current_run_token()
    try:
        requested = max(1, min(MAX_LIMIT, int(limit or nightly_limit())))
        started_at = outreach_tracking.utc_iso()
        run: Dict[str, Any] = {
            "started_at": started_at,
            "trigger": trigger,
            "dry_run": bool(dry_run),
            "requested": requested,
        }
        known = set(_known_handles(core))
        known_domains = _known_domains(core)
        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        seen_candidate_handles: set[str] = set()
        used_categories: set[str] = set()
        reviewer_feedback = _reviewer_feedback(core)
        rounds: List[Dict[str, Any]] = []
        all_focus: List[str] = []
        total_research_requested = 0
        total_returned = 0
        search_error = ""

        for round_number in range(1, max_search_rounds() + 1):
            missing = requested - len(accepted)
            if missing <= 0:
                break
            research_limit = _research_limit(missing)
            total_research_requested += research_limit
            focus, recent_categories = _rotation_for_run(
                core,
                offset=(round_number - 1) * _CATEGORIES_PER_RUN,
            )
            focus = [category for category in focus if category not in used_categories]
            if not focus:
                focus, _unused = _rotation_for_run(core, offset=round_number)
            for category in focus:
                if category not in all_focus:
                    all_focus.append(category)
            blocked_categories = list(dict.fromkeys(
                list(recent_categories) + sorted(used_categories)
            ))
            try:
                _set_run_phase(
                    token,
                    f"Search round {round_number}: filling {missing} remaining candidate"
                    f"{'s' if missing != 1 else ''}",
                )
                candidates, telemetry = _ask_for_candidates(
                    research_limit,
                    sorted(known),
                    focus=focus,
                    avoid_domains=sorted(known_domains),
                    avoid_categories=blocked_categories,
                    reviewer_feedback=reviewer_feedback,
                    model=model,
                )
            except Exception as exc:
                search_error = str(exc)[:400]
                if not accepted:
                    run.update({
                        "finished_at": outreach_tracking.utc_iso(),
                        "error": search_error,
                        "returned": total_returned,
                        "rounds": rounds,
                    })
                    _record_run(core, run)
                    raise
                break

            if not _owns_run(token):
                raise RuntimeError(
                    "This discovery run lost its lease before candidate checks completed"
                )

            candidate_total = len(candidates)
            total_returned += candidate_total
            accepted_before = len(accepted)
            round_rejected_before = len(rejected)
            for index, candidate in enumerate(
                candidates[:MAX_RESEARCH_CANDIDATES], start=1
            ):
                if len(accepted) >= requested:
                    break
                if not _owns_run(token):
                    raise RuntimeError(
                        "This discovery run lost its lease during candidate checks"
                    )
                _set_run_phase(
                    token,
                    f"Round {round_number}: checking candidate {index} of {candidate_total}; "
                    f"{requested - len(accepted)} slot(s) still open",
                )
                raw_handle = str(
                    (candidate or {}).get("storefront_handle") or ""
                ).strip().lower()
                raw_host = _domain((candidate or {}).get("organization_url"))
                if raw_handle and raw_handle in seen_candidate_handles:
                    continue
                if raw_handle:
                    seen_candidate_handles.add(raw_handle)
                payload, reason = _clean(
                    candidate,
                    known,
                    known_domains,
                    used_categories,
                )
                if payload is None:
                    rejected.append({
                        "handle": raw_handle[:80],
                        "reason": reason,
                    })
                    # Do not pay to research the same failed prospect again in
                    # a refill round, whatever wording the model gives it.
                    if _SAFE_HANDLE.fullmatch(raw_handle):
                        known.add(raw_handle)
                    if raw_host:
                        known_domains.add(raw_host)
                    continue

                row = {
                    "handle": payload["storefront_handle"],
                    "storefront_name": payload["storefront_name"],
                    "contact_email": payload["contact_email"],
                    "organization_url": payload["organization_url"],
                    "logo_source_url": payload["logo_source_url"],
                    "category": payload["category"],
                    "type_of_store": payload["type_of_store"],
                    "estimated_members": payload["estimated_members"],
                    "fit_evidence": payload["fit_evidence"],
                    "merchandise_check": payload["merchandise_check"],
                    "why_it_qualifies": str(
                        (candidate or {}).get("why_it_qualifies") or ""
                    )[:300],
                }
                if dry_run:
                    row["queued"] = False
                    # Kept so the candidates just read on screen can be built as
                    # they are. Searching again to build them would cost another
                    # search and could return a different set.
                    row["payload"] = payload
                else:
                    ok, status = _submit(core, payload)
                    if not ok:
                        rejected.append({
                            "handle": payload["storefront_handle"],
                            "reason": f"intake submission failed: {status}",
                        })
                        known.add(payload["storefront_handle"])
                        host = _domain(payload["organization_url"])
                        if host:
                            known_domains.add(host)
                        continue
                    row["queued"] = True
                    row["status"] = status

                accepted.append(row)
                used_categories.add(payload["category"])
                known.add(payload["storefront_handle"])
                host = _domain(payload["organization_url"])
                if host:
                    known_domains.add(host)

            gained = len(accepted) - accepted_before
            rounds.append({
                **telemetry,
                "round": round_number,
                "requested": research_limit,
                "returned": candidate_total,
                "accepted": gained,
                "rejected": len(rejected) - round_rejected_before,
                "remaining": max(0, requested - len(accepted)),
            })
            if len(accepted) >= requested:
                break

        input_tokens = sum(int(item.get("input_tokens") or 0) for item in rounds)
        output_tokens = sum(int(item.get("output_tokens") or 0) for item in rounds)
        elapsed = round(sum(float(item.get("seconds") or 0) for item in rounds), 1)
        complete = len(accepted) == requested

        run.update({
            "finished_at": outreach_tracking.utc_iso(),
            "focus": all_focus,
            "reviewer_feedback": reviewer_feedback,
            "research_requested": total_research_requested,
            "search_rounds": len(rounds),
            "rounds": rounds,
            "returned": total_returned,
            "accepted": len(accepted),
            "queued": sum(1 for row in accepted if row.get("queued")),
            "rejected": rejected,
            "candidates": accepted,
            "complete": complete,
            "shortfall": max(0, requested - len(accepted)),
            "shortfall_reason": (
                "" if complete else (
                    search_error
                    or f"The public-web search could not fill every slot after {len(rounds)} refill rounds."
                )
            ),
            "attempts": sum(int(item.get("attempts") or 0) for item in rounds),
            "model": next((item.get("model") for item in rounds if item.get("model")), model),
            "search_tool": next((item.get("search_tool") for item in rounds if item.get("search_tool")), ""),
            "seconds": elapsed,
            "input_tokens": input_tokens or None,
            "output_tokens": output_tokens or None,
        })
        _record_run(core, run)
        return run
    finally:
        _end_run(token)


def build_last_run(core: Any) -> Dict[str, Any]:
    """Build the candidates from the most recent test run.

    Duplicates are re-checked rather than trusted: the test run may have been
    minutes or hours ago, and a store could have been created since.
    """
    history = recent_runs(core)
    latest = history[0] if history else None
    if not latest or not latest.get("dry_run"):
        raise LookupError("The last run was not a test run")
    rows = [row for row in (latest.get("candidates") or []) if row.get("payload")]
    if not rows:
        raise LookupError("That test run has no candidates left to build")

    known = set(_known_handles(core))
    known_domains = _known_domains(core)
    built: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for row in rows:
        payload = row["payload"]
        handle = str(payload.get("storefront_handle") or "")
        host = _domain(payload.get("organization_url"))
        if handle in known or (host and host in known_domains):
            skipped.append({"handle": handle, "reason": "already known"})
            continue
        ok, status = _submit(core, payload)
        known.add(handle)
        if host:
            known_domains.add(host)
        built.append({
            "handle": handle,
            "storefront_name": payload.get("storefront_name"),
            "queued": ok,
            "status": status,
        })

    run = {
        "started_at": outreach_tracking.utc_iso(),
        "finished_at": outreach_tracking.utc_iso(),
        "trigger": "build-reviewed",
        "dry_run": False,
        "model": latest.get("model"),
        "requested": len(rows),
        "returned": len(rows),
        "accepted": len(built),
        "queued": sum(1 for row in built if row.get("queued")),
        "rejected": skipped,
        "candidates": built,
    }
    _record_run(core, run)
    return run


def _start_manual_run(
    core: Any,
    *,
    token: str,
    limit: int,
    dry_run: bool,
    model: str,
) -> None:
    """Run discovery after the HTTP response has returned.

    The review page polls status. Holding its POST request open for search,
    verification, and retries invites browser timeouts and makes people press
    Run again, which is how a healthy slow run looked frozen.
    """
    def worker() -> None:
        try:
            run_discovery(
                core,
                limit=limit,
                dry_run=dry_run,
                trigger="manual",
                model=model,
                _reserved_token=token,
            )
        except Exception:
            # The run records its own error before raising. Keep a server-side
            # traceback too, without turning an expected provider failure into
            # a silent background-thread death.
            print("[outreach-discovery] manual run failed", flush=True)
            import traceback
            traceback.print_exc()

    thread = threading.Thread(
        target=worker,
        name="outreach-discovery",
        daemon=True,
    )
    thread.start()


def install_outreach_discovery_routes(app: Any, core: Any) -> bool:
    app_id = id(app)
    with _INSTALL_LOCK:
        if app_id in _INSTALLED_APP_IDS:
            return False
        _INSTALLED_APP_IDS.add(app_id)

    @app.get("/api/outreach/discovery/status")
    def discovery_status(request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        return {
            "ok": True,
            "configured": configured(),
            "nightly_enabled": enabled(),
            "nightly_limit": nightly_limit(),
            "model": os.getenv("OUTREACH_DISCOVERY_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            "research_limit": _research_limit(nightly_limit()),
            **_run_snapshot(),
            "runs": recent_runs(core),
        }

    @app.post("/api/outreach/discovery/run")
    async def discovery_run(request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not configured():
            return JSONResponse(
                {"ok": False, "error": "OPENAI_API_KEY is not configured"},
                status_code=409,
            )
        try:
            limit = int((body or {}).get("limit") or nightly_limit())
        except (TypeError, ValueError):
            limit = nightly_limit()
        dry_run = bool((body or {}).get("dry_run"))
        model = str((body or {}).get("model") or "").strip()[:80]
        if model and not _SAFE_MODEL.fullmatch(model):
            return JSONResponse({"ok": False, "error": "invalid model id"}, status_code=400)
        allowed, age = _begin_run()
        if not allowed:
            snapshot = _run_snapshot()
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "A discovery run has been going for "
                        f"{int(age // 60)}m {int(age % 60)}s"
                    ),
                    **snapshot,
                },
                status_code=409,
            )
        token = _current_run_token()
        _start_manual_run(
            core,
            token=token,
            limit=limit,
            dry_run=dry_run,
            model=model,
        )
        return JSONResponse(
            {"ok": True, "started": True, **_run_snapshot()},
            status_code=202,
        )

    @app.post("/api/outreach/discovery/clear")
    def discovery_clear(request: Request):
        """Forget a run that is stuck, without a redeploy.

        The takeover deadline handles the common case on its own. This is for
        the moment you know it is dead and would rather not wait out the
        remainder of twenty minutes.
        """
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        snapshot = _run_snapshot()
        if snapshot["running"] and not snapshot["can_clear"]:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "The current run is still within its bounded wait window.",
                    **snapshot,
                },
                status_code=409,
            )
        return {"ok": True, "cleared": clear_stuck_run()}

    @app.post("/api/outreach/discovery/build-last")
    async def discovery_build_last(request: Request):
        denied = require_outreach_secret(core, request)
        if denied is not None:
            return denied
        try:
            run = await run_in_threadpool(build_last_run, core)
        except LookupError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:400]}, status_code=502)
        return {"ok": True, "run": run}

    @app.post("/api/outreach/discovery/cron")
    def discovery_cron(request: Request):
        denied = core._require_cron_secret(request)
        if denied is not None:
            return denied
        if not configured() or not enabled():
            # Not an error. The switch is off, and a cron that shouts about it
            # every night trains you to ignore it.
            return {"ok": True, "skipped": "discovery is not enabled"}
        try:
            run = run_discovery(core, trigger="nightly")
        except RuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)[:400]}, status_code=502)
        return {"ok": True, "run": run}

    return True
