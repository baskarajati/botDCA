from decimal import Decimal

from botdca.exchange import OpenOrder
from botdca.protection import ProtectionStatus, assess_protection, repaired


def _tp(order_id="tp-1", qty=0.3, price=81.0, link="botdca-tp-abc") -> OpenOrder:
    return OpenOrder(order_id, link, "HYPEUSDT", "Sell", "Limit", price, qty, True, "New")


def test_a_flat_symbol_has_nothing_to_protect() -> None:
    assessment = assess_protection(symbol="HYPEUSDT", position_qty=0.0, open_orders=[])
    assert assessment.status is ProtectionStatus.NOT_APPLICABLE
    assert assessment.healthy


def test_open_position_with_a_matching_take_profit_is_protected() -> None:
    assessment = assess_protection(
        symbol="HYPEUSDT",
        position_qty=0.3,
        open_orders=[_tp(qty=0.3)],
        expected_qty=Decimal("0.3"),
    )
    assert assessment.status is ProtectionStatus.PROTECTED
    assert assessment.healthy
    assert not assessment.requires_intervention


def test_open_position_without_any_take_profit_is_explicitly_unhealthy() -> None:
    assessment = assess_protection(symbol="HYPEUSDT", position_qty=0.3, open_orders=[])
    assert assessment.status is ProtectionStatus.MISSING
    assert not assessment.healthy
    assert "no resting bot take profit" in assessment.detail


def test_a_partially_covering_take_profit_counts_as_missing_protection() -> None:
    assessment = assess_protection(
        symbol="HYPEUSDT",
        position_qty=0.5,
        open_orders=[_tp(qty=0.3)],
        expected_qty=Decimal("0.5"),
    )
    assert assessment.status is ProtectionStatus.MISSING
    assert assessment.protected_qty == 0.3


def test_duplicate_take_profits_require_intervention_rather_than_another_order() -> None:
    assessment = assess_protection(
        symbol="HYPEUSDT",
        position_qty=0.3,
        open_orders=[_tp("tp-1"), _tp("tp-2", link="botdca-tp-def")],
        expected_qty=Decimal("0.3"),
    )
    assert assessment.status is ProtectionStatus.AMBIGUOUS
    assert assessment.requires_intervention
    assert not assessment.healthy


def test_non_bot_and_non_reduce_only_sell_orders_are_not_protection() -> None:
    foreign = OpenOrder("x", "manual-1", "HYPEUSDT", "Sell", "Limit", 81.0, 0.3, True, "New")
    not_reduce_only = OpenOrder(
        "y", "botdca-tp-xyz", "HYPEUSDT", "Sell", "Limit", 81.0, 0.3, False, "New"
    )
    dca = OpenOrder("z", "botdca-dca-1", "HYPEUSDT", "Buy", "Limit", 78.0, 0.4, False, "New")

    assessment = assess_protection(
        symbol="HYPEUSDT",
        position_qty=0.3,
        open_orders=[foreign, not_reduce_only, dca],
    )
    assert assessment.status is ProtectionStatus.MISSING


def test_repaired_marks_the_position_as_covered_again() -> None:
    missing = assess_protection(symbol="HYPEUSDT", position_qty=0.3, open_orders=[])
    fixed = repaired(missing, "protection rebuilt")
    assert fixed.status is ProtectionStatus.REPAIRED
    assert fixed.protected_qty == 0.3
    assert fixed.describe()["detail"] == "protection rebuilt"


def test_a_take_profit_that_matches_the_plan_must_still_cover_the_position() -> None:
    """The plan is not the yardstick; the position is.

    A planned quantity that is itself short of the position agrees with the
    resting order, so comparing the two calls the basket protected while part of
    it has no exit at all.
    """
    assessment = assess_protection(
        symbol="HYPEUSDT",
        position_qty=0.90,
        open_orders=[_tp(qty=0.89)],
        expected_qty=0.89,
    )

    assert assessment.status is ProtectionStatus.MISSING
    assert not assessment.healthy
    assert "unprotected" in assessment.detail
    assert assessment.protected_qty == 0.89


def test_a_take_profit_larger_than_the_position_is_not_reported_as_missing() -> None:
    """Over-coverage is a different problem; reduce-only caps the fill anyway."""
    assessment = assess_protection(
        symbol="HYPEUSDT",
        position_qty=0.30,
        open_orders=[_tp(qty=0.30)],
        expected_qty=0.30,
    )

    assert assessment.status is ProtectionStatus.PROTECTED
