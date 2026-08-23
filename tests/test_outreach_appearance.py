from __future__ import annotations

import json

import pytest

import outreach_appearance


class FakeCore:
    def __init__(self, shop=True):
        self.shop = {"id": "gid://shopify/Metaobject/1", "fields": {}} if shop else None
        self.calls = []

    def _get_custom_shop(self, _handle):
        return self.shop

    def _shopify_graphql(self, query, variables):
        self.calls.append((query, variables))
        return {"metaobjectUpdate": {"metaobject": {"id": "gid://shopify/Metaobject/1"},
                                     "userErrors": []}}


def _state(**overrides):
    state = {
        "handle": "vero-beach-vfd",
        "storefront_name": "Vero Beach Volunteer Fire Department Team Store",
        "type_of_store": "volunteer fire department",
        "brand_colors": ["#b6001e", "#1f2a44", "#f5f2e8"],
    }
    state.update(overrides)
    return state


def test_the_store_wears_the_logos_own_colours():
    settings = outreach_appearance.settings_for(_state())
    assert settings["primary_color"] == "#b6001e"
    assert settings["secondary_color"] == "#1f2a44"


def test_text_stays_readable_on_whatever_colour_came_out():
    """A dark red needs white text and a cream needs black. Getting this wrong
    is an unreadable button on every product card."""
    dark = outreach_appearance.settings_for(_state(brand_colors=["#151515"]))
    light = outreach_appearance.settings_for(_state(brand_colors=["#f5f2e8"]))

    assert dark["primary_text"] == "#ffffff"
    assert light["primary_text"] == "#111111"


def test_a_logo_with_no_usable_colour_still_makes_a_coherent_store():
    settings = outreach_appearance.settings_for(_state(brand_colors=[]))
    assert settings["primary_color"] == outreach_appearance._FALLBACK_PRIMARY
    assert settings["secondary_color"] == outreach_appearance._FALLBACK_SECONDARY


def test_junk_colours_are_not_written_into_the_storefront():
    settings = outreach_appearance.settings_for(
        _state(brand_colors=["red", "#zzzzzz", "#b6001e"])
    )
    assert settings["primary_color"] == "#b6001e"


def test_the_greeting_is_about_them_not_about_groups_in_general():
    """Every store used to read "Approved gear for your group", which is true
    of all of them and therefore says nothing about any of them."""
    fire = outreach_appearance.welcome_message(_state())
    assert "Vero Beach Volunteer Fire Department" in fire
    assert "Team Store" not in fire

    rescue = outreach_appearance.welcome_message(
        _state(storefront_name="Second Chance Dog Rescue Team Store",
               type_of_store="dog rescue")
    )
    assert "animals" in rescue
    assert rescue != fire


def test_a_sports_club_gets_sideline_words_not_department_words():
    soccer = outreach_appearance.welcome_message(
        _state(storefront_name="Riverside Youth Soccer Club Team Store",
               type_of_store="youth soccer club")
    )
    assert "sideline" in soccer


def test_the_greeting_fits_the_field():
    long_name = "A" * 300
    settings = outreach_appearance.settings_for(_state(storefront_name=long_name))
    assert len(settings["welcome_message"]) <= 180


def test_applying_it_writes_the_settings_field():
    core = FakeCore()
    assert outreach_appearance.apply(core, "vero-beach-vfd", _state()) is True

    _query, variables = core.calls[0]
    field = variables["metaobject"]["fields"][0]
    assert field["key"] == "storefront_settings"
    written = json.loads(field["value"])
    assert written["primary_color"] == "#b6001e"
    assert written["enabled"] is True


def test_a_missing_store_is_reported_not_raised():
    """A store that is built and unstyled is worth having. Failing the build
    over a colour is not."""
    assert outreach_appearance.apply(FakeCore(shop=False), "gone", _state()) is False


def test_shopify_refusing_the_write_does_not_raise():
    class Refusing(FakeCore):
        def _shopify_graphql(self, _query, _variables):
            return {"metaobjectUpdate": {"metaobject": None,
                                         "userErrors": [{"message": "nope"}]}}

    assert outreach_appearance.apply(Refusing(), "vero-beach-vfd", _state()) is False


def test_shopify_being_down_does_not_raise():
    class Broken(FakeCore):
        def _shopify_graphql(self, _query, _variables):
            raise RuntimeError("Shopify unavailable")

    assert outreach_appearance.apply(Broken(), "vero-beach-vfd", _state()) is False


# ---- which design the store opens on --------------------------------------


def test_an_unattended_store_gets_the_boldest_design(monkeypatch):
    """The pair that was being sent — clean/none — is what an unset store
    falls through to, so a store carrying the organization's own colours was
    wearing the plainest design available."""
    monkeypatch.delenv("OUTREACH_STORE_LAYOUT", raising=False)
    settings = outreach_appearance.settings_for(_state())

    assert (settings["style"], settings["pattern"]) == \
        outreach_appearance.LAYOUTS[outreach_appearance.DEFAULT_LAYOUT]
    assert (settings["style"], settings["pattern"]) != ("clean", "none")


def test_the_design_can_be_changed_without_a_deploy(monkeypatch):
    """Which design looks best is judged by looking at a store, and needing a
    deploy between each look is how the plain one stayed."""
    monkeypatch.setenv("OUTREACH_STORE_LAYOUT", "gradient")
    settings = outreach_appearance.settings_for(_state())
    assert (settings["style"], settings["pattern"]) == ("bold", "none")

    monkeypatch.setenv("OUTREACH_STORE_LAYOUT", "spray")
    settings = outreach_appearance.settings_for(_state())
    assert (settings["style"], settings["pattern"]) == ("bold", "dots")


def test_a_nonsense_layout_name_still_builds_a_store(monkeypatch):
    monkeypatch.setenv("OUTREACH_STORE_LAYOUT", "sparkles")
    settings = outreach_appearance.settings_for(_state())
    assert (settings["style"], settings["pattern"]) == \
        outreach_appearance.LAYOUTS[outreach_appearance.DEFAULT_LAYOUT]


def test_every_layout_name_maps_to_a_pair_the_storefront_knows():
    """These pairs are matched literally in private-store-layout-state.liquid.
    A pair that is not in that list silently renders as the plain design."""
    known = {
        ("clean", "none"), ("clean", "diagonal"), ("clean", "stripes"),
        ("bold", "none"), ("bold", "dots"), ("dark", "grid"),
    }
    for name, pair in outreach_appearance.LAYOUTS.items():
        assert pair in known, f"{name} maps to {pair}, which the storefront does not match"
