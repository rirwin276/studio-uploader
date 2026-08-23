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
from urllib.parse import urlparse

import requests


_TIMEOUT = (5, 12)
_MAX_HTML = 400_000
_UA = "Stella-Sage-Outreach-Verify/1.0"

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
