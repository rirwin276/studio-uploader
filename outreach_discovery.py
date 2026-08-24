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
        return {
            "running": started is not None,
            "age_seconds": round(age, 1) if started is not None else None,
            "phase": _RUN_PHASE or None,
            "updated_at_epoch": _RUN_UPDATED_AT,
            "stale_after_seconds": _STALE_RUN_SECONDS,
            "can_clear": started is not None and age >= _STALE_RUN_SECONDS,
        }


def _begin_run() -> Tuple[bool, float]:
    """Claim the right to run. Returns (allowed, age of the run in the way)."""
    global _RUN_STARTED_AT, _RUN_TOKEN, _RUN_PHASE, _RUN_UPDATED_AT
    now = time.time()
    with _RUN_STATE_LOCK:
        started = _RUN_STARTED_AT
        if started is not None:
            age = now - started
            if age < _STALE_RUN_SECONDS:
                return False, age
            print(
                f"[outreach-discovery] taking over from a run started {age / 60:.0f} "
                "minutes ago — treating it as dead"
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

# Left to its own devices the model finds one easy vein and stays in it — two
# runs in a row of nothing but volunteer fire departments. The brief listing
# every category is not enough, because listing is not the same as steering.
# Each run is pointed at a slice of this list instead, so variety comes from
# the schedule rather than from hoping.
CATEGORY_ROTATION = [
    "independent youth soccer, baseball, softball, basketball and lacrosse clubs",
    "rowing, crew, sailing, canoe, kayak and dragon-boat clubs",
    "independent swim, dive, water-polo and masters swim clubs",
    "martial arts, judo, wrestling, boxing and fencing clubs",
    "archery, disc-golf, climbing and orienteering clubs",
    "local cycling, running, trail-running and triathlon clubs",
    "dance studios, competitive cheer clubs and gymnastics clubs",
    "small volunteer fire departments, EMS and search-and-rescue squads",
    "small animal, dog and equine rescues with local volunteers",
    "community theater groups, local choirs and small orchestras",
]
_CATEGORIES_PER_RUN = 3
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
                    "storefront_name",
                    "storefront_handle",
                    "type_of_store",
                    "primary_color",
                    "contact_email",
                    "organization_url",
                    "contact_source_url",
                    "logo_source_url",
                    "why_it_qualifies",
                ],
                "properties": {
                    "storefront_name": {"type": "string"},
                    "storefront_handle": {"type": "string"},
                    "type_of_store": {"type": "string"},
                    "primary_color": {"type": "string"},
                    "contact_email": {"type": "string"},
                    "organization_url": {"type": "string"},
                    "contact_source_url": {"type": "string"},
                    "logo_source_url": {"type": "string"},
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
        shown = "\n".join("- " + item for item in focus)
        focus_line = (
            "\n\nFocus this run on these kinds of organization:\n" + shown +
            "\n\nReturn at most one organization per kind, and vary the state or "
            "region between them. If one kind has nothing good, return fewer rather "
            "than filling the list from whichever kind was easiest to search."
        )
    return f"""Find up to {limit} organizations that would plausibly want team apparel and do not currently sell it online.

Who qualifies:
- A small, independent local group with its own identity and roughly 20–500 members: youth sports clubs, rowing/sailing/paddling clubs, swim teams, martial arts/fencing/archery clubs, dance/gymnastics clubs, local run/cycle clubs, volunteer rescue groups, small animal rescues, or community arts groups.
- They have a public website with a visible logo.
- They have a contact email address published on their own website.
- They do NOT already sell apparel online. Check for a Store, Shop, Merch, Spirit Wear or Gear page. If they have one, skip them.
- Prefer United States organizations.

Skip immediately:
- Anything with an existing online store, even a bad one.
- Schools, school districts, PTOs, booster clubs tied to a school, churches, Scout troops, military posts, municipal/county departments, universities, large nonprofits, franchises, national brands and pro teams.
- Organizations that are a program inside a larger institution rather than an independent local group with its own identity.
- Anyone whose only contact is a web form with no email address.
- Anyone whose logo you cannot find as a direct image file on their own site.
- Anyone you are not confident is a real, currently active organization.

Hard rules:
- Only use information publicly visible on the organization's own website.
- Never guess an email address or construct one from a pattern. If you cannot see a real published address, skip the organization.
- logo_source_url should be the exact direct image URL copied from the official site's HTML or image link. Never invent a conventional path such as /logo.png, /images/logo.png, or /assets/logo.svg. The system will also extract and verify the official site's header logo when needed.
- storefront_handle: lowercase letters, numbers and hyphens only. "St. Mary's Rowing" becomes st-marys-rowing.
- storefront_name: the organization name followed by " Team Store".
- primary_color: one common color name from their branding (Navy, Red, Royal Blue, Forest Green, Maroon, Black, Charcoal, Purple, Orange, Gold).
- why_it_qualifies: one sentence, including where you checked for an existing store and what you found.

Aim to return all {limit} candidates. Do not stop after the first two or three
that look promising: continue through the focus categories until you have
reached the target, unless every remaining organization fails one of the hard
rules. Never fill the list by guessing a contact, logo, or store status.
{focus_line}{avoid_line}"""


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


def _rotation_for_run(core: Any) -> Tuple[List[str], List[str]]:
    """Which categories to chase this run, and which to stay off.

    Stepping through the list by run count means variety is a property of the
    schedule rather than something the model has to be talked into.
    """
    history = recent_runs(core)
    cursor = len(history) * _CATEGORIES_PER_RUN
    focus = [
        CATEGORY_ROTATION[(cursor + offset) % len(CATEGORY_ROTATION)]
        for offset in range(min(_CATEGORIES_PER_RUN, len(CATEGORY_ROTATION)))
    ]
    recent: List[str] = []
    for run in history[:_RECENT_CATEGORY_RUNS]:
        for row in run.get("candidates") or []:
            kind = str(row.get("type_of_store") or "").strip()
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
) -> Tuple[Dict[str, Any] | None, str]:
    """Re-check the model's work before it can create anything.

    The brief already says all of this. That is not the same as it being true,
    and a bad handle or a page-instead-of-an-image logo is cheaper to reject
    here than to inspect in Shopify afterwards.
    """
    if not isinstance(candidate, dict):
        return None, "not an object"
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

    return {
        "provider_request_id": f"discovery-{time.strftime('%Y%m%d')}-{handle}"[:120],
        "source_agent": "openai",
        "contact_email": email,
        "storefront_name": str(candidate.get("storefront_name") or "").strip()[:300],
        "storefront_handle": handle,
        "type_of_store": str(candidate.get("type_of_store") or "").strip()[:120],
        "primary_color": str(candidate.get("primary_color") or "").strip()[:60],
        "screening_confirmed": True,
        "logo_source_reviewed": True,
        "email_authorized": False,
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
        research_limit = _research_limit(requested)
        started_at = outreach_tracking.utc_iso()
        run: Dict[str, Any] = {
            "started_at": started_at,
            "trigger": trigger,
            "dry_run": bool(dry_run),
            "requested": requested,
            "research_requested": research_limit,
        }
        known = set(_known_handles(core))
        known_domains = _known_domains(core)
        focus, avoid_categories = _rotation_for_run(core)
        run["focus"] = focus
        try:
            _set_run_phase(token, f"Searching public sources for up to {research_limit} candidates")
            candidates, telemetry = _ask_for_candidates(
                research_limit,
                sorted(known),
                focus=focus,
                avoid_domains=sorted(known_domains),
                avoid_categories=avoid_categories,
                model=model,
            )
            run.update(telemetry)
        except Exception as exc:
            run["finished_at"] = outreach_tracking.utc_iso()
            run["error"] = str(exc)[:400]
            run["returned"] = 0
            _record_run(core, run)
            raise

        if not _owns_run(token):
            raise RuntimeError("This discovery run lost its lease before candidate checks completed")

        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        candidate_total = len(candidates)
        for index, candidate in enumerate(candidates[:MAX_RESEARCH_CANDIDATES], start=1):
            if len(accepted) >= requested:
                break
            if not _owns_run(token):
                raise RuntimeError("This discovery run lost its lease during candidate checks")
            _set_run_phase(token, f"Checking candidate {index} of {candidate_total}")
            payload, reason = _clean(candidate, known, known_domains)
            if payload is None:
                rejected.append({
                    "handle": str((candidate or {}).get("storefront_handle") or "")[:80],
                    "reason": reason,
                })
                continue
            known.add(payload["storefront_handle"])
            host = _domain(payload["organization_url"])
            if host:
                known_domains.add(host)
            row = {
                "handle": payload["storefront_handle"],
                "storefront_name": payload["storefront_name"],
                "contact_email": payload["contact_email"],
                "organization_url": payload["organization_url"],
                "logo_source_url": payload["logo_source_url"],
                "type_of_store": payload["type_of_store"],
                "why_it_qualifies": str((candidate or {}).get("why_it_qualifies") or "")[:300],
            }
            if dry_run:
                row["queued"] = False
                # Kept so the candidates just read on screen can be built as
                # they are. Searching again to build them would cost another
                # search and could return a different set — the reviewed ones
                # should be the ones that get made.
                row["payload"] = payload
            else:
                ok, status = _submit(core, payload)
                row["queued"] = ok
                row["status"] = status
            accepted.append(row)

        run.update({
            "finished_at": outreach_tracking.utc_iso(),
            "returned": len(candidates),
            "accepted": len(accepted),
            "queued": sum(1 for row in accepted if row.get("queued")),
            "rejected": rejected,
            "candidates": accepted,
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
