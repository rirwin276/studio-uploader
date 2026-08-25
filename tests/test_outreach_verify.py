from __future__ import annotations

import pytest

import outreach_verify


class Response:
    def __init__(self, html="", status=200, url="https://example.org/"):
        self.status_code = status
        self.encoding = "utf-8"
        self.url = url
        self._html = html.encode()

    @property
    def raw(self):
        outer = self

        class Raw:
            def read(self, _n, decode_content=True):
                return outer._html

        return Raw()

    def close(self):
        pass


@pytest.fixture
def site(monkeypatch):
    pages = {}

    def get(url, **_kwargs):
        if url not in pages:
            raise ConnectionError("no route to host")
        page = pages[url]
        if isinstance(page, int):
            return Response(status=page, url=url)
        return Response(page, url=url)

    monkeypatch.setattr(outreach_verify.requests, "get", get)
    return pages


# ---- is the organization real? --------------------------------------------


def test_a_real_site_about_the_real_organization_passes(site):
    site["https://westside-rowing.org/"] = (
        "<html><h1>Westside Rowing Club</h1><p>Junior crew since 1978.</p></html>"
    )
    ok, _why = outreach_verify.organization_is_real(
        "https://westside-rowing.org/", "Westside Rowing Club Team Store"
    )
    assert ok


def test_a_site_that_never_mentions_them_is_refused(site):
    """A model that invents an organization invents its website to match.
    Going and looking is the cheapest way to tell."""
    site["https://example.org/"] = "<html><h1>Domain for sale</h1></html>"
    ok, why = outreach_verify.organization_is_real(
        "https://example.org/", "Undefined Robotics Team Store"
    )
    assert not ok
    assert "undefined" in why or "robotics" in why


def test_a_dead_site_is_refused(site):
    site["https://gone.org/"] = 404
    ok, why = outreach_verify.organization_is_real("https://gone.org/", "Gone Rowing Club")
    assert not ok
    assert "404" in why


def test_an_unreachable_site_is_not_treated_as_a_lie(site):
    """The network is unreliable and a timeout is not evidence. Only a definite
    negative should cost a candidate."""
    ok, why = outreach_verify.organization_is_real(
        "https://unreachable.org/", "Westside Rowing Club"
    )
    assert ok
    assert "not checked" in why


def test_generic_words_alone_cannot_vouch_for_a_site(site):
    """Matching on "team" or "club" would let any page pass for any
    organization."""
    site["https://something-else.org/"] = "<html><p>Our club and team store</p></html>"
    ok, _why = outreach_verify.organization_is_real(
        "https://something-else.org/", "Westside Rowing Club Team Store"
    )
    assert not ok


def test_script_contents_do_not_count_as_a_mention(site):
    """Analytics blobs and JSON payloads mention all sorts of things."""
    site["https://other.org/"] = (
        "<html><script>var x='westside rowing';</script><h1>Different Club</h1></html>"
    )
    ok, _why = outreach_verify.organization_is_real(
        "https://other.org/", "Westside Rowing Club"
    )
    assert not ok


def test_a_name_with_nothing_distinctive_is_not_blocked(site):
    site["https://x.org/"] = "<html><h1>Hello</h1></html>"
    ok, _why = outreach_verify.organization_is_real("https://x.org/", "The Team Store")
    assert ok


# ---- finding the real logo in the official page ---------------------------


def test_header_logo_is_preferred_over_hero_and_social_images():
    html = '''
        <img class="hero-banner" src="/photos/team.jpg">
        <img id="site-logo" alt="Westside Rowing logo" src="/assets/crest.svg">
        <meta property="og:image" content="https://westside-rowing.org/photos/social.jpg">
    '''
    urls = outreach_verify._logo_candidates_from_html(html, "https://westside-rowing.org/")
    assert urls[0] == "https://westside-rowing.org/assets/crest.svg"


def test_relative_and_lazy_header_logo_urls_are_found():
    html = '<img data-src="images/logo.png" class="site-header">'
    urls = outreach_verify._logo_candidates_from_html(html, "https://club.org/about")
    assert urls == ["https://club.org/images/logo.png"]


def test_largest_official_srcset_logo_is_preferred():
    html = '''
        <img class="site-header logo" src="/logo-120.png"
             srcset="/logo-240.png 240w, /logo-1200.png 1200w">
    '''
    urls = outreach_verify._logo_candidates_from_html(html, "https://club.org/")
    assert urls[0] == "https://club.org/logo-1200.png"


# ---- existing merchandise and obvious scale -------------------------------


def test_an_official_merch_link_disqualifies_the_candidate(site):
    site["https://club.org/"] = '<nav><a href="/team-store">Team Store</a></nav>'
    found, why = outreach_verify.existing_merchandise("https://club.org/")
    assert found
    assert "merchandise" in why


def test_an_external_merch_vendor_link_disqualifies_the_candidate(site):
    site["https://club.org/"] = (
        '<a href="https://club.squadlocker.com/">Order uniforms and shirts</a>'
    )
    found, _why = outreach_verify.existing_merchandise("https://club.org/")
    assert found


def test_normal_contact_and_registration_links_are_not_mistaken_for_merch(site):
    site["https://club.org/"] = (
        '<a href="/contact">Contact</a><a href="/registration">Registration</a>'
    )
    found, why = outreach_verify.existing_merchandise("https://club.org/")
    assert not found
    assert why == ""


def test_a_site_advertising_hundreds_of_members_is_too_large(site):
    site["https://club.org/"] = '<p>Serving over 850 athletes across the region.</p>'
    fits, why = outreach_verify.small_group_fit("https://club.org/")
    assert not fits
    assert "850" in why


def test_a_plain_small_club_site_passes_the_scale_gate(site):
    site["https://club.org/"] = '<p>Our local rowing club practices twice weekly.</p>'
    fits, why = outreach_verify.small_group_fit("https://club.org/")
    assert fits
    assert why == ""


# ---- finding a published contact email ------------------------------------


def test_homepage_email_is_used_as_public_contact_evidence(site):
    site["https://club.org/"] = '<a href="mailto:team@club.org">Email us</a>'

    addresses, why = outreach_verify.published_contact_emails("https://club.org/")

    assert why == ""
    assert addresses == [("team@club.org", "https://club.org/")]


def test_contact_page_email_is_found_when_homepage_has_none(site):
    site["https://club.org/"] = '<a href="/contact">Contact the club</a>'
    site["https://club.org/contact"] = '<p>Info@club.org</p>'

    addresses, why = outreach_verify.published_contact_emails("https://club.org/")

    assert why == ""
    assert addresses == [("info@club.org", "https://club.org/contact")]


# ---- can the address receive mail? ----------------------------------------


def test_a_domain_that_resolves_can_receive(monkeypatch):
    monkeypatch.setattr(outreach_verify, "_mx_or_a", lambda _d: True)
    ok, _why = outreach_verify.email_can_receive("info@westside-rowing.org")
    assert ok


def test_a_domain_that_does_not_exist_is_refused(monkeypatch):
    """A bounced first email costs sender reputation, which is the one asset
    here that is slow to rebuild — and it is damaged before anybody reads a
    word."""
    monkeypatch.setattr(outreach_verify, "_mx_or_a", lambda _d: False)
    ok, why = outreach_verify.email_can_receive("info@westside-rowinng.org")
    assert not ok
    assert "bounce" in why


def test_something_that_is_not_an_address_is_refused():
    assert outreach_verify.email_can_receive("not-an-address")[0] is False
    assert outreach_verify.email_can_receive("someone@localhost")[0] is False
    assert outreach_verify.email_can_receive("")[0] is False


# ---- both together ---------------------------------------------------------


def test_the_email_is_checked_before_the_website(monkeypatch, site):
    """The DNS lookup is the cheaper of the two, so a candidate with a dead
    address never costs an HTTP request."""
    monkeypatch.setattr(outreach_verify, "_mx_or_a", lambda _d: False)
    keep, why = outreach_verify.check_candidate({
        "contact_email": "info@nowhere.invalid",
        "organization_url": "https://never-fetched.org/",
        "storefront_name": "Westside Rowing Club",
    })
    assert not keep
    assert "bounce" in why


def test_a_good_candidate_survives_both(monkeypatch, site):
    monkeypatch.setattr(outreach_verify, "_mx_or_a", lambda _d: True)
    site["https://westside-rowing.org/"] = "<html><h1>Westside Rowing Club</h1></html>"
    keep, why = outreach_verify.check_candidate({
        "contact_email": "info@westside-rowing.org",
        "organization_url": "https://westside-rowing.org/",
        "storefront_name": "Westside Rowing Club Team Store",
    })
    assert keep, why
