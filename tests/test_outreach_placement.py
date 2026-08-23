from __future__ import annotations

import importlib


def _profile(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import outreach_direct
    return importlib.reload(outreach_direct).DEFAULT_PLACEMENT_PROFILE


def test_youth_garments_get_more_lift_than_adult_ones(monkeypatch):
    """A youth body is shorter, so the same print zone puts the art nearer the
    middle of the chest than the top of it."""
    profile = _profile(monkeypatch)

    assert profile["bc3001y_front_vertical_offset_px"] < 0
    assert profile["cc1467y_front_vertical_offset_px"] < profile["bc3413_front_vertical_offset_px"]


def test_the_youth_tee_is_no_longer_the_one_garment_that_cannot_move(monkeypatch):
    """It had no offset at all, which is why its logo sat lowest of the set."""
    assert "bc3001y_front_vertical_offset_px" in _profile(monkeypatch)


def test_an_offset_can_be_retuned_without_a_deploy(monkeypatch):
    """Print placement is judged by looking at a mockup, not by reasoning about
    percentages, so it has to be adjustable between looks."""
    profile = _profile(monkeypatch, BC3001Y_FRONT_VERTICAL_OFFSET_PX="-450")
    assert profile["bc3001y_front_vertical_offset_px"] == -450


def test_a_nonsense_override_falls_back_instead_of_crashing_the_build(monkeypatch):
    profile = _profile(monkeypatch, BC3001Y_FRONT_VERTICAL_OFFSET_PX="up a bit")
    assert profile["bc3001y_front_vertical_offset_px"] == -300
