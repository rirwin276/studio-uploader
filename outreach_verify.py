"""Checking the model's homework before a store gets built.

Discovery asserts things — this organization exists, this address receives
mail, they have no shop yet. The brief asks for all of it and none of it was
ever verified, which is how a store got built for "Undefined Robotics" and
how a typo'd address would be discovered by bouncing off a real mail server.

Both checks here are free, deterministic and fast. Neither asks a model
anything, because the model is what is being checked.

Failures are advisory by default: the network is unreliable and a timeout is
not evidence of anything. Only a definite negative — a site that says it is
gone, a domain that accepts no mail at all — rejects a candidate.
"""

from __future__ import annotations

import re
import socket
from typing import Any, Dict, List, Tuple
from urllib.parse import urljoin, urlparse

import requests


_TIMEOUT = (5, 12)
_MAX_HTML = 400_000
_UA = "Stella-Sage-Outreach-Verify/1.0"
_MAX_LOGO_CANDIDATES = 24
_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)

# Words that carry no identifying weight, so matching on them would let any
# page pass for any organization.
_STOPWORDS = {
    "the", "of", "and", "for", "a", "an", "at", "in", "on", "to",
    "team", "store", "club", "association", "organization", "organisation",
    "inc", "llc", "foundation", "society", "group", "center", "centre",
    "department", "county", "city", "valley", "community", "youth", "junior",
}


def _significant_words(name: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    return [word for word in words if len(word) > 2 and word not in _STOPWORDS]


def _text_of(html: str) -> str:
    without_scripts = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I
    )
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", without_scripts)).lower()


def _html_attributes(tag: str) -> Dict[str, str]:
    """Small, dependency-free attribute reader for the few tags we need.

    These sites range from hand-written pages to site builders. Pulling in a
    browser or a full HTML parser just to find the header logo makes this job
    slower and more fragile than the discovery itself.
    """
    values: Dict[str, str] = {}
    for match in re.finditer(
        r'''([:\w-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>`]+))''', tag, re.I
    ):
        values[match.group(1).lower()] = next(
            (part for part in match.groups()[1:] if part is not None), ""
        )
    return values


def _emails_from_html(html: str) -> List[str]:
    """Emails visibly published in a page's markup, in document order."""
    # Script/json payloads frequently contain tracking addresses and are not
    # something a visitor can reasonably be said to have been shown.
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    seen = set()
    values: List[str] = []
    for match in _EMAIL_RE.finditer(text):
        email = match.group(0).strip().lower().rstrip(".,;:)")
        if email not in seen:
            seen.add(email)
            values.append(email)
    return values


def _contact_page_links(html: str, page_url: str) -> List[str]:
    """A small bounded list of likely first-party contact pages."""
    values: List[str] = []
    seen = set()
    for tag in re.findall(r"<a\b[^>]*>.*?</a>", html, flags=re.S | re.I):
        attrs = _html_attributes(tag)
        raw = attrs.get("href", "")
        label = re.sub(r"<[^>]+>", " ", tag).lower()
        marker = (raw + " " + label).lower()
        if not raw or not any(word in marker for word in ("contact", "about", "staff", "team")):
            continue
        absolute = urljoin(page_url, raw)
        if not absolute.lower().startswith("https://") or absolute in seen:
            continue
        seen.add(absolute)
        values.append(absolute)
        if len(values) >= 3:
            break
    return values


def _read_html_page(url: str) -> Tuple[str, str, str]:
    """Fetch one public page for lightweight evidence extraction."""
    try:
        response = requests.get(
            url,
            timeout=_TIMEOUT,
            headers={"User-Agent": _UA},
            allow_redirects=True,
            stream=True,
        )
    except Exception as exc:
        return "", url, f"could not be read ({type(exc).__name__})"
    try:
        if response.status_code >= 400:
            return "", str(response.url or url), f"returned HTTP {response.status_code}"
        html = response.raw.read(_MAX_HTML, decode_content=True).decode(
            response.encoding or "utf-8", errors="replace"
        )
        return html, str(response.url or url), ""
    except Exception:
        return "", str(response.url or url), "could not be read"
    finally:
        response.close()


def published_contact_emails(organization_url: str) -> Tuple[List[Tuple[str, str]], str]:
    """Find a real, visible contact email on the official site.

    The candidate model is excellent at finding an organization but not a
    reliable source of record for its email. This keeps it from guessing while
    rescuing a prospect whose published address lives on `/contact` instead of
    the home page.
    """
    html, page_url, issue = _read_html_page(organization_url)
    if not html:
        return [], f"organization page {issue}".strip()
    found = [(email, page_url) for email in _emails_from_html(html)]
    if found:
        return found, ""
    for contact_url in _contact_page_links(html, page_url):
        contact_html, resolved_url, _contact_issue = _read_html_page(contact_url)
        emails = _emails_from_html(contact_html) if contact_html else []
        if emails:
            return [(email, resolved_url) for email in emails], ""
    return [], "no usable contact email published on the official site"


def _logo_candidates_from_html(html: str, page_url: str) -> List[str]:
    """Return likely direct logo images referenced by the official page.

    Models regularly identify the right organization and then manufacture a
    conventional-looking `/logo.png`. The public page itself is the source of
    truth: header logo image first, then its declared social/logo image as a
    last resort. Every returned URL is still downloaded and verified later.
    """
    ranked: List[Tuple[int, int, str]] = []
    order = 0

    def add(raw_url: str, context: str, base_score: int) -> None:
        nonlocal order
        raw_url = str(raw_url or "").strip()
        if not raw_url or raw_url.startswith("data:"):
            return
        absolute = urljoin(page_url, raw_url)
        if not absolute.lower().startswith("https://"):
            return
        lower = (context + " " + absolute).lower()
        score = base_score
        if "logo" in lower:
            score += 200
        if any(word in lower for word in ("brand", "identity", "site-header", "navbar")):
            score += 60
        if any(word in lower for word in ("favicon", "avatar", "social-icon", "sprite")):
            score -= 160
        if any(word in lower for word in ("hero", "banner", "carousel", "gallery", "background")):
            score -= 35
        ranked.append((score, order, absolute))
        order += 1

    for tag in re.findall(r"<(?:img|source)\b[^>]*>", html, flags=re.I):
        attrs = _html_attributes(tag)
        context = " ".join(attrs.values())
        # Lazy-loading builders use any of these. srcset is deliberately not
        # used: selecting one of several responsive sources needs browser
        # rules, while their normal src/data-src is the stable original.
        for field in ("src", "data-src", "data-original", "data-lazy-src"):
            if attrs.get(field):
                add(attrs[field], context, 40)

    for tag in re.findall(r"<(?:meta|link)\b[^>]*>", html, flags=re.I):
        attrs = _html_attributes(tag)
        marker = " ".join((attrs.get("property", ""), attrs.get("name", ""), attrs.get("rel", ""))).lower()
        if marker in {"og:image", "twitter:image", "image_src"} or "logo" in marker:
            add(attrs.get("content") or attrs.get("href") or "", " ".join(attrs.values()), 5)

    seen = set()
    result: List[str] = []
    for _score, _position, url in sorted(ranked, key=lambda item: (-item[0], item[1])):
        if url not in seen:
            seen.add(url)
            result.append(url)
        if len(result) >= _MAX_LOGO_CANDIDATES:
            break
    return result


def logo_sources_on_organization_site(url: str) -> Tuple[List[str], str]:
    """Find actual image URLs embedded by an organization's home page.

    This deliberately does not accept a search engine's image result. The URL
    must be present in the organization's own markup, and the caller then
    applies the existing origin and binary-image checks before it can build.
    """
    try:
        response = requests.get(
            url,
            timeout=_TIMEOUT,
            headers={"User-Agent": _UA},
            allow_redirects=True,
            stream=True,
        )
    except Exception as exc:
        return [], f"organization page could not be read ({type(exc).__name__})"
    try:
        if response.status_code >= 400:
            return [], f"organization page returned HTTP {response.status_code}"
        html = response.raw.read(_MAX_HTML, decode_content=True).decode(
            response.encoding or "utf-8", errors="replace"
        )
        urls = _logo_candidates_from_html(html, str(response.url or url))
        if not urls:
            return [], "organization page did not expose a usable logo image"
        return urls, ""
    except Exception:
        return [], "organization page could not be read"
    finally:
        response.close()


def organization_is_real(url: str, organization_name: str) -> Tuple[bool, str]:
    """Whether that website exists and is about that organization.

    A model that invents an organization also invents its website, and the
    cheapest way to tell is to go and look. Matching is on the distinctive
    words in the name — "Undefined Robotics" has to have "undefined" or
    "robotics" somewhere on its own front page, or it is not their site.
    """
    name_words = _significant_words(organization_name)
    if not name_words:
        return True, "no distinctive words in the name to check"

    try:
        response = requests.get(
            url,
            timeout=_TIMEOUT,
            headers={"User-Agent": _UA},
            allow_redirects=True,
            stream=True,
        )
    except Exception as exc:
        # The site being unreachable from here is not proof it is not real.
        return True, f"could not be reached ({type(exc).__name__}) — not checked"

    if response.status_code >= 400:
        return False, f"their website returned HTTP {response.status_code}"

    try:
        html = response.raw.read(_MAX_HTML, decode_content=True).decode(
            response.encoding or "utf-8", errors="replace"
        )
    except Exception:
        return True, "page could not be read — not checked"
    finally:
        response.close()

    haystack = _text_of(html) + " " + str(response.url or "").lower()
    hits = [word for word in name_words if word in haystack]
    if hits:
        return True, f"site mentions {', '.join(hits[:3])}"
    return False, (
        f"their website never mentions {' or '.join(name_words[:3])}, so it is "
        "probably not their site"
    )


def _mx_or_a(domain: str) -> bool:
    """Whether anything at this domain could accept mail.

    A domain with no MX record can still receive on its A record, so both
    count. Only a domain that resolves to nothing at all is a definite no.
    """
    try:
        import dns.resolver  # type: ignore

        try:
            if dns.resolver.resolve(domain, "MX"):
                return True
        except Exception:
            pass
        try:
            return bool(dns.resolver.resolve(domain, "A"))
        except Exception:
            return False
    except ImportError:
        # No dnspython on this deployment. A plain hostname lookup still tells
        # us whether the domain exists, which catches the typo case.
        try:
            socket.getaddrinfo(domain, None)
            return True
        except socket.gaierror:
            return False
        except Exception:
            return True


def email_can_receive(address: str) -> Tuple[bool, str]:
    """Whether this address's domain could take delivery.

    Nothing is sent. A bounced first email costs sender reputation, which is
    the one asset here that is slow to rebuild — and unlike the content, it is
    damaged before anybody reads a word.
    """
    address = str(address or "").strip().lower()
    if "@" not in address:
        return False, "not an email address"
    domain = address.rsplit("@", 1)[-1].strip(".")
    if not domain or "." not in domain:
        return False, f"{domain or 'that address'} is not a deliverable domain"

    if _mx_or_a(domain):
        return True, ""
    return False, f"{domain} does not accept mail — the address would bounce"


def check_candidate(candidate: Dict[str, Any]) -> Tuple[bool, str]:
    """Both checks, cheapest first. Returns (keep, why not)."""
    ok, why = email_can_receive(candidate.get("contact_email"))
    if not ok:
        return False, why

    ok, why = organization_is_real(
        candidate.get("organization_url"),
        candidate.get("storefront_name") or candidate.get("storefront_handle"),
    )
    if not ok:
        return False, why
    return True, ""


def logo_source_is_live(url: str) -> Tuple[bool, str]:
    """Prove that a proposed logo URL is a public image before a build starts.

    A language model can correctly identify an organization's web site and
    still invent the conventional-looking path ``/logo.png``. The intake
    downloader already has the right public-URL, redirect, content-type and
    size protections, so discovery uses that same code path rather than a
    weaker HEAD request that would only move the failure to the build queue.
    """
    try:
        from outreach_intake import OutreachIntakeError, _download_public_image

        _download_public_image(str(url or ""), 12 * 1024 * 1024)
    except OutreachIntakeError as exc:
        return False, str(exc)
    except Exception as exc:
        # Do not allow an unexpected fetch failure to turn into a prospective
        # store with an unknown asset. The reason is retained in the run log.
        return False, f"logo source could not be verified ({type(exc).__name__})"
    return True, ""
