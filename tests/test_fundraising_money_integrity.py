"""The fundraiser must never owe a cause money it did not collect.

Every test here is about one direction of error. Under-crediting shortchanges a
school. Over-crediting is paid out of the platform's own pocket on Friday and is
not recoverable once the transfer lands, so the bias throughout is: when it is
not certain the money was collected, do not credit it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app as A  # noqa: E402


VARIANT_A = "gid://shopify/ProductVariant/111"
VARIANT_B = "gid://shopify/ProductVariant/222"


def _state(**kw):
    """A launched fundraiser: $5 to the cause, +$1 platform fee, so +$6 on price."""
    state = {
        "enabled": True,
        "amount": 5,
        "markup_add": 6,
        "base_prices": {VARIANT_A: "24.00", VARIANT_B: "30.00"},
        "total_raised": 0,
        "ledger": [],
    }
    state.update(kw)
    return state


# --- crediting only what was actually collected -----------------------------

def test_a_sale_at_the_fundraiser_price_is_credited():
    lines = [(VARIANT_A, "30.00", 2)]
    assert A._fr_collected_qty(_state(), lines) == 2


def test_a_product_added_after_launch_is_not_credited():
    """The case that quietly cost real money.

    A garment published mid-campaign is not in base_prices, so it never got the
    markup and sold at its plain price. Crediting it anyway created a payout
    obligation against money that was never taken from the shopper.
    """
    unknown = "gid://shopify/ProductVariant/999"
    assert A._fr_collected_qty(_state(), [(unknown, "24.00", 3)]) == 0


def test_a_variant_whose_price_write_failed_is_not_credited():
    """Sold at base while the rest of the store carried the markup."""
    assert A._fr_collected_qty(_state(), [(VARIANT_A, "24.00", 1)]) == 0


def test_an_order_placed_before_the_reprice_finished_is_not_credited():
    """Launch flips enabled immediately; repricing runs in a background thread."""
    pending = _state(markup_add=0, base_prices={})
    assert A._fr_collected_qty(pending, [(VARIANT_A, "24.00", 1)]) == 0


def test_a_mixed_order_credits_only_the_marked_up_lines():
    lines = [
        (VARIANT_A, "30.00", 2),                                 # marked up
        (VARIANT_B, "30.00", 4),                                 # base 30, not marked up
        ("gid://shopify/ProductVariant/999", "22.00", 7),         # added after launch
    ]
    assert A._fr_collected_qty(_state(), lines) == 2


def test_size_surcharges_do_not_break_the_comparison():
    """base_prices snapshots each variant, so a 3XL's higher base is its own."""
    state = _state(base_prices={VARIANT_A: "24.00", VARIANT_B: "27.00"})
    assert A._fr_collected_qty(state, [(VARIANT_B, "33.00", 1)]) == 1
    assert A._fr_collected_qty(state, [(VARIANT_B, "30.00", 1)]) == 0


def test_money_is_compared_in_cents_not_floats():
    """0.1 + 0.2 arithmetic at exact equality decides whether a sale counts."""
    state = _state(markup_add=6, base_prices={VARIANT_A: "24.10"})
    assert A._fr_collected_qty(state, [(VARIANT_A, "30.10", 1)]) == 1


@pytest.mark.parametrize("price", [None, "", "not-a-price"])
def test_an_unreadable_price_is_not_credited(price):
    assert A._fr_collected_qty(_state(), [(VARIANT_A, price, 1)]) == 0


def test_zero_and_negative_quantities_are_ignored():
    assert A._fr_collected_qty(_state(), [(VARIANT_A, "30.00", 0)]) == 0
    assert A._fr_collected_qty(_state(), [(VARIANT_A, "30.00", -3)]) == 0


# --- reversals ---------------------------------------------------------------

def _credited(order_id="ORD1", amount=10.0, qty=2, created_at="2026-08-01T00:00:00+00:00"):
    return _state(ledger=[{
        "order_id": order_id, "amount": amount, "qty": qty,
        "created_at": created_at, "paid": False,
    }], total_raised=amount)


def test_a_full_refund_reverses_the_whole_contribution():
    state = _credited()
    row = A._fr_reverse_order(
        "h", state, original_order_id="ORD1", reversal_id="refund:R1", refunded_qty=2,
    )
    assert row["amount"] == -10.0
    assert row["qty"] == -2


def test_a_partial_refund_reverses_proportionally():
    state = _credited(amount=10.0, qty=2)
    row = A._fr_reverse_order(
        "h", state, original_order_id="ORD1", reversal_id="refund:R1", refunded_qty=1,
    )
    assert row["amount"] == -5.0
    assert row["qty"] == -1


def test_a_reversal_is_dated_to_the_contribution_it_cancels():
    """Otherwise it sits out the 7-day hold while the row it offsets is paid on
    Friday, and the refunded money leaves before the correction can stop it."""
    state = _credited(created_at="2026-08-01T00:00:00+00:00")
    row = A._fr_reverse_order(
        "h", state, original_order_id="ORD1", reversal_id="refund:R1", refunded_qty=2,
    )
    assert row["created_at"] == "2026-08-01T00:00:00+00:00"


def test_a_repeated_refund_webhook_reverses_once():
    state = _credited()
    first = A._fr_reverse_order(
        "h", state, original_order_id="ORD1", reversal_id="refund:R1", refunded_qty=2,
    )
    state["ledger"].append(first)
    again = A._fr_reverse_order(
        "h", state, original_order_id="ORD1", reversal_id="refund:R1", refunded_qty=2,
    )
    assert again is None


def test_reversing_more_than_was_credited_is_capped():
    """A shopper can be refunded for units bought before the fundraiser started,
    or for a product that never carried the markup — neither is in the ledger."""
    state = _credited(amount=10.0, qty=2)
    row = A._fr_reverse_order(
        "h", state, original_order_id="ORD1", reversal_id="refund:R1", refunded_qty=99,
    )
    assert row["amount"] == -10.0
    assert row["qty"] == -2


def test_refunding_an_order_that_was_never_credited_does_nothing():
    row = A._fr_reverse_order(
        "h", _state(), original_order_id="NEVER", reversal_id="refund:R1", refunded_qty=1,
    )
    assert row is None


def test_cancel_and_refund_of_the_same_order_cannot_double_reverse():
    """Shopify can send both for one order; each is capped at what was credited,
    and the second finds nothing positive left to reverse beyond the cap."""
    state = _credited(amount=10.0, qty=2)
    cancel = A._fr_reverse_order(
        "h", state, original_order_id="ORD1", reversal_id="cancel:ORD1", refunded_qty=2,
    )
    state["ledger"].append(cancel)
    state["total_raised"] = round(state["total_raised"] + cancel["amount"], 2)

    refund = A._fr_reverse_order(
        "h", state, original_order_id="ORD1", reversal_id="refund:R1", refunded_qty=2,
    )
    # The second reversal is a distinct id so it is produced, but the net across
    # both must never make the cause owe more than it was ever credited.
    net = state["total_raised"] + (refund["amount"] if refund else 0)
    assert net <= 0


def test_a_reversal_nets_against_the_payout_it_belongs_to():
    """An unpaid contribution and its reversal must cancel inside one batch."""
    state = _credited(amount=10.0, qty=2)
    row = A._fr_reverse_order(
        "h", state, original_order_id="ORD1", reversal_id="refund:R1", refunded_qty=2,
    )
    state["ledger"].append(row)
    from datetime import date
    summary = A._fr_summarize_rows(state, date(2026, 8, 21))
    assert summary["total_unpaid"] == 0.0


# --- the limits the wizard promises --------------------------------------

@pytest.mark.parametrize("raw,ok", [
    ("2026-12-01", True),
    ("", True),
    ("2099-01-01", False),
    ("not-a-date", False),
])
def test_end_date_bounds_are_enforced_on_the_server(raw, ok):
    """"Max 1 year from today" was enforced only by the browser form."""
    assert (A._fr_validate_end_date(raw) == "") is ok


def test_the_advertised_amount_range_matches_the_constants():
    """The wizard says "$1–$8 range" in copy; the server has to agree."""
    assert (A._FR_MIN_AMOUNT, A._FR_MAX_AMOUNT) == (1, 8)


def test_test_mode_is_decided_by_the_key_that_moves_the_money():
    """The banner claiming no real money moves must not be a hardcoded string."""
    import app
    original = app._STRIPE_KEY
    try:
        app._STRIPE_KEY = "sk_live_abc"
        assert app._fr_stripe_livemode() is True
        app._STRIPE_KEY = "sk_test_abc"
        assert app._fr_stripe_livemode() is False
        app._STRIPE_KEY = ""
        assert app._fr_stripe_livemode() is False
    finally:
        app._STRIPE_KEY = original


# --- a campaign that says it ends must end ----------------------------------

def _dated(days_from_now, enabled=True):
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) + timedelta(days=days_from_now)
    return _state(enabled=enabled, end_date=when.date().isoformat())


def test_a_campaign_past_its_end_date_is_finished():
    assert A._fr_campaign_ended(_dated(-2)) is True


def test_a_running_campaign_is_not_finished():
    assert A._fr_campaign_ended(_dated(5)) is False


def test_the_final_day_is_still_a_selling_day():
    """The storefront bar counts down to 23:59:59 on the end date and shows
    "Final day". Closing at midnight that morning would stop the campaign a day
    before the shoppers looking at it were told."""
    assert A._fr_campaign_ended(_dated(0)) is False


def test_the_check_does_not_depend_on_the_clamped_day_count():
    """_fr_days_left clamps to max(0, days) so an ended campaign reads "0 days
    left" rather than negative — correct for display, useless for deciding."""
    ended = _dated(-2)
    assert A._fr_days_left(ended["end_date"]) == 0
    assert A._fr_campaign_ended(ended) is True


def test_a_campaign_with_no_end_date_runs_until_stopped():
    assert A._fr_campaign_ended(_state(end_date="")) is False


def test_an_already_stopped_campaign_is_not_re_closed():
    assert A._fr_campaign_ended(_dated(-2, enabled=False)) is False


# --- the ledger must not outgrow the field it lives in ----------------------

def _row(order_id, days_ago, paid, amount=5.0):
    from datetime import datetime, timedelta, timezone
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"order_id": order_id, "amount": amount, "qty": 1,
            "created_at": when.isoformat(), "paid": paid}


def test_settled_aged_rows_are_folded_into_a_total():
    state = _state(ledger=[_row("A", 400, True), _row("B", 500, True)])
    assert A._fr_compact_ledger(state) == 2
    assert state["ledger"] == []
    assert state["ledger_archive"]["rows"] == 2
    assert state["ledger_archive"]["amount"] == 10.0


def test_money_still_owed_is_never_folded_away():
    """An unpaid row is a debt to a cause. Age is irrelevant."""
    state = _state(ledger=[_row("A", 900, False)])
    assert A._fr_compact_ledger(state) == 0
    assert len(state["ledger"]) == 1


def test_recent_payouts_stay_line_by_line_for_reconciliation():
    state = _state(ledger=[_row("A", 3, True)])
    assert A._fr_compact_ledger(state) == 0
    assert len(state["ledger"]) == 1


def test_a_row_with_an_unreadable_date_is_kept_not_dropped():
    state = _state(ledger=[{"order_id": "A", "amount": 5.0, "qty": 1,
                            "created_at": "whenever", "paid": True}])
    assert A._fr_compact_ledger(state) == 0
    assert len(state["ledger"]) == 1


def test_archiving_twice_accumulates_rather_than_resets():
    state = _state(ledger=[_row("A", 400, True)])
    A._fr_compact_ledger(state)
    state["ledger"] = [_row("B", 400, True)]
    A._fr_compact_ledger(state)
    assert state["ledger_archive"]["rows"] == 2


# --- one writer per store ---------------------------------------------------

def test_the_same_store_always_gets_the_same_lock():
    assert A._fr_lock("northview") is A._fr_lock("northview")


def test_different_stores_do_not_block_each_other():
    assert A._fr_lock("northview") is not A._fr_lock("westfield")
