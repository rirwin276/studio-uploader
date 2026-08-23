from __future__ import annotations

import pytest

import outreach_mail
import outreach_mockups


class FakeCore:
    def __init__(self, titles):
        self.titles = titles
        self.queries = []

    def _shopify_graphql(self, _query, variables):
        self.queries.append(variables)
        return {"products": {"edges": [
            {"node": {"title": title,
                      "featuredImage": {"url": f"https://cdn.shopify.com/{n}.jpg",
                                        "altText": title}}}
            for n, title in enumerate(self.titles)
        ]}}


@pytest.fixture
def downloads(monkeypatch):
    fetched = []

    class Response:
        status_code = 200
        headers = {"Content-Type": "image/jpeg"}

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield b"\xff\xd8fake-jpeg-bytes"

    def get(url, **_kw):
        fetched.append(url)
        return Response()

    monkeypatch.setattr(outreach_mockups.requests, "get", get)
    return fetched


def test_it_picks_a_tee_and_a_hoodie(downloads):
    """The email names a tri-blend tee and a hoodie, so those are the two the
    prospect should be looking at."""
    core = FakeCore([
        "Westside Rowing Tank Top",
        "Westside Rowing Unisex Tri-Blend Tee",
        "Westside Rowing Youth Tee",
        "Westside Rowing Premium Pullover Hoodie",
    ])
    photos = outreach_mockups.for_email(core, "westside-rowing")

    assert [photo["title"] for photo in photos] == [
        "Westside Rowing Unisex Tri-Blend Tee",
        "Westside Rowing Premium Pullover Hoodie",
    ]


def test_a_store_without_a_hoodie_still_sends_two_photos(downloads):
    """A slot that cannot be filled as intended is filled with something. The
    prospect cannot tell which garment the picker was hoping for."""
    core = FakeCore(["Club Tri-Blend Tee", "Club Tank Top", "Club Youth Tee"])
    photos = outreach_mockups.for_email(core, "club")

    assert len(photos) == 2
    assert photos[0]["title"] == "Club Tri-Blend Tee"


def test_the_same_product_is_never_sent_twice(downloads):
    """One garment photographed once is one photo, not a pair."""
    core = FakeCore(["Club Tri-Blend Tee"])
    photos = outreach_mockups.for_email(core, "club")
    assert len(photos) == 1


def test_only_that_store_is_searched(downloads):
    core = FakeCore(["Club Tri-Blend Tee"])
    outreach_mockups.for_email(core, "club")
    assert core.queries[0]["query"] == "tag:club"


def test_a_product_with_no_image_is_skipped(downloads):
    class Imageless(FakeCore):
        def _shopify_graphql(self, _query, _variables):
            return {"products": {"edges": [
                {"node": {"title": "Club Tri-Blend Tee", "featuredImage": None}},
                {"node": {"title": "Club Hoodie",
                          "featuredImage": {"url": "https://cdn.shopify.com/h.jpg"}}},
            ]}}

    photos = outreach_mockups.for_email(Imageless([]), "club")
    assert [photo["title"] for photo in photos] == ["Club Hoodie"]


def test_the_cdn_is_asked_for_an_email_sized_copy(downloads):
    """Shopify serves these at print resolution, and a four megabyte email is
    a deliverability problem by itself."""
    outreach_mockups.for_email(FakeCore(["Club Tri-Blend Tee"]), "club")
    assert "width=720" in downloads[0]


def test_shopify_being_down_does_not_stop_the_email(downloads):
    class Broken(FakeCore):
        def _shopify_graphql(self, _query, _variables):
            raise RuntimeError("Shopify unavailable")

    assert outreach_mockups.for_email(Broken([]), "club") == []


def test_one_failed_download_does_not_lose_the_other_photo(monkeypatch):
    calls = {"n": 0}

    class Response:
        headers = {"Content-Type": "image/jpeg"}

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield b"bytes"

    def get(_url, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("connection reset")
        return Response()

    monkeypatch.setattr(outreach_mockups.requests, "get", get)
    core = FakeCore(["Club Tri-Blend Tee", "Club Hoodie"])
    photos = outreach_mockups.for_email(core, "club")

    assert [photo["title"] for photo in photos] == ["Club Hoodie"]


def test_an_oversized_image_is_dropped_rather_than_mailed(monkeypatch):
    class Huge:
        headers = {"Content-Type": "image/jpeg"}

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            for _ in range(40):
                yield b"x" * 64_000

    monkeypatch.setattr(outreach_mockups.requests, "get", lambda _u, **_k: Huge())
    assert outreach_mockups.for_email(FakeCore(["Club Tri-Blend Tee"]), "club") == []


def test_something_that_is_not_an_image_is_refused(monkeypatch):
    class Html:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield b"<!doctype html>"

    monkeypatch.setattr(outreach_mockups.requests, "get", lambda _u, **_k: Html())
    assert outreach_mockups.for_email(FakeCore(["Club Tri-Blend Tee"]), "club") == []


# ---- the composed message -------------------------------------------------


def _state():
    return {
        "handle": "westside-rowing",
        "storefront_name": "Westside Rowing Team Store",
        "contact_email": "info@westside.org",
    }


def _photos(count=2):
    return [
        {"title": f"Garment {n}", "alt": f"Garment {n}",
         "data": b"\xff\xd8bytes", "subtype": "jpeg"}
        for n in range(count)
    ]


def test_the_photos_ride_along_inside_the_message(monkeypatch):
    """Attached rather than linked. A first email from an unknown sender has
    its remote images blocked by most clients, and a blocked hero image is a
    broken-looking email."""
    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    message = outreach_mail.first_contact(_state(), _photos())

    images = [part for part in message.walk()
              if part.get_content_maintype() == "image"]
    assert len(images) == 2
    for image in images:
        assert image.get("Content-ID")
        assert image.get_payload(decode=True) == b"\xff\xd8bytes"


def test_every_photo_is_actually_referenced_by_the_html(monkeypatch):
    """An attached image with nothing pointing at it shows up as a mystery
    attachment instead of a picture."""
    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    message = outreach_mail.first_contact(_state(), _photos())

    html = [part for part in message.walk()
            if part.get_content_type() == "text/html"][0].get_content()
    cids = [part.get("Content-ID").strip("<>") for part in message.walk()
            if part.get_content_maintype() == "image"]

    assert cids
    for cid in cids:
        assert f"cid:{cid}" in html


def test_the_plain_text_version_is_still_a_complete_email(monkeypatch):
    """Plenty of people read plain text, and the version without pictures has
    to stand on its own — links included."""
    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    message = outreach_mail.first_contact(_state(), _photos())

    text = [part for part in message.walk()
            if part.get_content_type() == "text/plain"][0].get_content()

    assert "collections/westside-rowing" in text
    assert "join-store?shop=westside-rowing" in text
    assert "1627 Honey Hill Rd" in text
    assert 'reply "no thanks"' in text


def test_without_photos_it_stays_the_plain_email_it_was(monkeypatch):
    """No pictures means no reason to be multipart."""
    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    message = outreach_mail.first_contact(_state())

    assert message.get_content_type() == "text/plain"
    assert "collections/westside-rowing" in message.get_content()


def test_the_opt_out_and_address_survive_into_the_html(monkeypatch):
    """CAN-SPAM applies to the version somebody actually looks at."""
    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    message = outreach_mail.first_contact(_state(), _photos())

    html = [part for part in message.walk()
            if part.get_content_type() == "text/html"][0].get_content()

    assert "1627 Honey Hill Rd" in html
    assert "no thanks" in html


def test_an_organization_name_cannot_inject_markup(monkeypatch):
    """The name comes from a web search, and it lands in an HTML document."""
    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    state = _state()
    state["storefront_name"] = "<script>alert(1)</script> Rowing Team Store"
    message = outreach_mail.first_contact(state, _photos())

    html = [part for part in message.walk()
            if part.get_content_type() == "text/html"][0].get_content()

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_a_plain_text_reader_is_told_the_photos_are_attached(monkeypatch):
    """In HTML the pictures speak for themselves. In plain text the same spot
    is a sentence introducing nothing unless it says where they went."""
    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    message = outreach_mail.first_contact(_state(), _photos())

    text = [part for part in message.walk()
            if part.get_content_type() == "text/plain"][0].get_content()
    html = [part for part in message.walk()
            if part.get_content_type() == "text/html"][0].get_content()

    assert "attached the two photos" in text
    # Redundant next to the pictures themselves.
    assert "attached the two photos" not in html


def test_the_garments_are_named_once(monkeypatch):
    """An email that says "a tri-blend tee and a hoodie" twice in consecutive
    sentences reads like it was assembled, not written."""
    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    message = outreach_mail.first_contact(_state(), _photos())

    text = [part for part in message.walk()
            if part.get_content_type() == "text/plain"][0].get_content()
    assert text.count("tri-blend tee and a hoodie") == 1


def test_the_follow_up_shows_one_garment_not_two(monkeypatch):
    """They have already read the pitch. The garment is the part worth
    repeating; a second copy of the same email is not."""
    monkeypatch.setenv("OUTREACH_FROM_EMAIL", "ryan@outreach.example.com")
    message = outreach_mail.follow_up(_state(), _photos())

    images = [part for part in message.walk()
              if part.get_content_maintype() == "image"]
    assert len(images) == 1
