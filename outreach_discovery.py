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
from typing import Any, Dict, List, Tuple

import requests
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import outreach_tracking
from outreach_auth import require_outreach_secret


# Run history lives in the same ledger as the stores, under a handle no real
# store can take, so there is one place to look and nothing new to provision.
RUN_LEDGER_HANDLE = "zz-discovery-runs"
_MAX_RUNS_KEPT = 25

_SAFE_HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_INSTALL_LOCK = threading.Lock()
_INSTALLED_APP_IDS: set[int] = set()
_RUN_LOCK = threading.Lock()

DEFAULT_LIMIT = 5
MAX_LIMIT = 10

# Left to its own devices the model finds one easy vein and stays in it — two
# runs in a row of nothing but volunteer fire departments. The brief listing
# every category is not enough, because listing is not the same as steering.
# Each run is pointed at a slice of this list instead, so variety comes from
# the schedule rather than from hoping.
CATEGORY_ROTATION = [
    "youth sports clubs — baseball, softball, soccer, basketball, lacrosse",
    "rowing, crew, sailing, canoe and kayak clubs",
    "swim and dive teams and masters swim clubs",
    "wrestling, martial arts, judo and boxing clubs",
    "volunteer fire departments, EMS and search-and-rescue squads",
    "school booster clubs, PTOs and band parent associations",
    "church youth groups, summer camps and scout troops",
    "dog rescues, animal shelters and equine rescues",
    "community theater, choir, orchestra and marching band groups",
    "veteran organizations, VFW and American Legion posts",
    "cycling, running, trail and triathlon clubs",
    "robotics teams, esports clubs and academic competition teams",
    "disc golf, archery, fencing, climbing and orienteering clubs",
    "food banks, community gardens and volunteer trail crews",
]
_CATEGORIES_PER_RUN = 3
_RECENT_CATEGORY_RUNS = 3

_API_URL = "https://api.openai.com/v1/responses"

# Both are environment-driven on purpose. Model names and the exact web-search
# tool identifier change on the provider's schedule, not ours, and a rename
# should be a variable edit rather than a deploy.
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
- Small or mid-size organizations: youth sports clubs, school teams, booster clubs, rowing/swim/wrestling clubs, volunteer fire departments, church youth groups, small nonprofits, community theater groups, dog rescues, veteran organizations.
- They have a public website with a visible logo.
- They have a contact email address published on their own website.
- They do NOT already sell apparel online. Check for a Store, Shop, Merch, Spirit Wear or Gear page. If they have one, skip them.
- Prefer United States organizations.

Skip immediately:
- Anything with an existing online store, even a bad one.
- Large national brands, franchises, universities, pro teams.
- Anyone whose only contact is a web form with no email address.
- Anyone whose logo you cannot find as a direct image file on their own site.
- Anyone you are not confident is a real, currently active organization.

Hard rules:
- Only use information publicly visible on the organization's own website.
- Never guess an email address or construct one from a pattern. If you cannot see a real published address, skip the organization.
- logo_source_url must point directly at an image file (.png, .svg, .jpg) on their own domain or CDN — not at a page containing a logo.
- storefront_handle: lowercase letters, numbers and hyphens only. "St. Mary's Rowing" becomes st-marys-rowing.
- storefront_name: the organization name followed by " Team Store".
- primary_color: one common color name from their branding (Navy, Red, Royal Blue, Forest Green, Maroon, Black, Charcoal, Purple, Orange, Gold).
- why_it_qualifies: one sentence, including where you checked for an existing store and what you found.

Quality over quantity. Returning three solid candidates is better than {limit} shaky ones. Return an empty list rather than anything you are unsure about.{focus_line}{avoid_line}"""


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
_MAX_ATTEMPTS = 3
_MAX_RETRY_WAIT = 30.0


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
    response = None
    attempts = 0
    for attempt in range(_MAX_ATTEMPTS):
        attempts = attempt + 1
        response = requests.post(
            _API_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=(15, 300),
        )
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

    email = str(candidate.get("contact_email") or "").strip()
    if "@" not in email or " " in email:
        return None, "no usable contact email"

    urls = {}
    for field in ("organization_url", "contact_source_url", "logo_source_url"):
        value = str(candidate.get(field) or "").strip()
        if not value.lower().startswith("https://"):
            return None, f"{field} is not a public https URL"
        urls[field] = value

    # Same organization, different handle. The website is the stable identity;
    # the name it gets called is not.
    host = _domain(urls["organization_url"])
    if host and host in (known_domains or set()):
        return None, f"{host} is already in the system"

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
) -> Dict[str, Any]:
    """Find candidates and, unless this is a dry run, queue them for building.

    A dry run does everything except create anything: it is how you check what
    the brief actually returns before letting it loose on a real Shopify store.
    """
    if not _RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("A discovery run is already in progress")
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
        focus, avoid_categories = _rotation_for_run(core)
        run["focus"] = focus
        try:
            candidates, telemetry = _ask_for_candidates(
                requested,
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

        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        for candidate in candidates[:requested]:
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
        _RUN_LOCK.release()


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
            "model": os.getenv("OUTREACH_DISCOVERY_MODEL", DEFAULT_MODEL),
            "running": _RUN_LOCK.locked(),
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
        try:
            # A run is a web search plus a model call: minutes of blocking HTTP.
            # Calling it inline from an async route holds the event loop for the
            # whole time, and this process serves the storefront — fundraiser
            # lookups, store status, the review queue — so a two-minute run made
            # the entire service look down rather than just busy.
            run = await run_in_threadpool(
                run_discovery,
                core,
                limit=limit,
                dry_run=dry_run,
                trigger="manual",
                model=model,
            )
        except RuntimeError as exc:
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
