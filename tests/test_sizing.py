from decimal import Decimal

import pytest

from botdca.instruments import InstrumentRules
from botdca.sizing import (
    InitialAllocation,
    SizingError,
    SizingMode,
    margin_for_quantity,
    quantity_for_margin,
    roi_percent,
    size_dca_order,
    size_initial_order,
)


def _rules(symbol: str = "HYPEUSDT", *, qty_step: str = "0.01", min_qty: str = "0.01") -> InstrumentRules:
    return InstrumentRules(
        symbol=symbol,
        tick_size=Decimal("0.01"),
        qty_step=Decimal(qty_step),
        min_order_qty=Decimal(min_qty),
        min_notional_value=Decimal(5),
        max_market_order_qty=Decimal(10000),
    )


def test_fixed_margin_sizing_uses_margin_times_leverage_over_price() -> None:
    sized = size_initial_order(
        InitialAllocation(SizingMode.FIXED_MARGIN_USDT, 1.0),
        _rules(),
        leverage=24,
        price=80.0,
    )
    # 1 USDT * 24 / 80 = 0.3
    assert sized.qty == Decimal("0.30")
    assert float(sized.notional_usdt) == pytest.approx(24.0)
    assert float(sized.margin_usdt) == pytest.approx(1.0)


def test_fixed_quantity_sizing_uses_the_configured_quantity_directly() -> None:
    sized = size_initial_order(
        InitialAllocation(SizingMode.FIXED_BASE_QUANTITY, 0.6),
        _rules(),
        leverage=24,
        price=83.0,
    )
    assert sized.qty == Decimal("0.60")
    assert float(sized.notional_usdt) == pytest.approx(49.8)
    # margin = qty * price / leverage
    assert float(sized.margin_usdt) == pytest.approx(49.8 / 24)


def test_fixed_quantity_and_fixed_margin_are_genuinely_different_configurations() -> None:
    quantity_sized = size_initial_order(
        InitialAllocation(SizingMode.FIXED_BASE_QUANTITY, 0.6),
        _rules(),
        leverage=24,
        price=83.0,
    )
    margin_sized = size_initial_order(
        InitialAllocation(SizingMode.FIXED_MARGIN_USDT, 1.0),
        _rules(),
        leverage=24,
        price=83.0,
    )
    # "0.6 HYPE" is not "1 USDT of margin"; conflating them would be a ~2x error.
    assert quantity_sized.qty != margin_sized.qty
    assert quantity_sized.margin_usdt > margin_sized.margin_usdt


def test_leverage_changes_margin_not_notional_at_a_fixed_quantity() -> None:
    at_24 = size_initial_order(
        InitialAllocation(SizingMode.FIXED_BASE_QUANTITY, 0.6), _rules(), leverage=24, price=83.0
    )
    at_30 = size_initial_order(
        InitialAllocation(SizingMode.FIXED_BASE_QUANTITY, 0.6), _rules(), leverage=30, price=83.0
    )
    assert at_24.notional_usdt == at_30.notional_usdt
    assert at_30.margin_usdt < at_24.margin_usdt


def test_leverage_changes_notional_at_a_fixed_margin() -> None:
    at_24 = size_initial_order(
        InitialAllocation(SizingMode.FIXED_MARGIN_USDT, 2.0), _rules(), leverage=24, price=83.0
    )
    at_30 = size_initial_order(
        InitialAllocation(SizingMode.FIXED_MARGIN_USDT, 2.0), _rules(), leverage=30, price=83.0
    )
    assert at_30.notional_usdt > at_24.notional_usdt


def test_quantity_is_floored_to_the_instrument_step() -> None:
    sized = size_initial_order(
        InitialAllocation(SizingMode.FIXED_BASE_QUANTITY, 0.6789),
        _rules(qty_step="0.01"),
        leverage=24,
        price=100.0,
    )
    assert sized.qty == Decimal("0.67")


def test_dca_quantity_applies_the_multiplier_then_instrument_rounding() -> None:
    sized = size_dca_order(0.30, 1.42, _rules(qty_step="0.01"), leverage=24, price=79.0)
    # 0.30 * 1.42 = 0.426 -> floored to the 0.01 step
    assert sized.qty == Decimal("0.42")


def test_allocation_below_the_instrument_minimum_is_refused_not_rounded_up() -> None:
    with pytest.raises(SizingError, match="instrument limits"):
        size_initial_order(
            InitialAllocation(SizingMode.FIXED_BASE_QUANTITY, 0.005),
            _rules(min_qty="0.01"),
            leverage=24,
            price=100.0,
        )


def test_order_below_minimum_notional_is_refused() -> None:
    with pytest.raises(SizingError, match="below .* minimum"):
        size_initial_order(
            InitialAllocation(SizingMode.FIXED_BASE_QUANTITY, 0.02),
            _rules(),
            leverage=24,
            price=100.0,
        )


def test_allocation_rejects_non_positive_and_non_finite_values() -> None:
    for bad in (0.0, -1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="positive and finite"):
            InitialAllocation(SizingMode.FIXED_MARGIN_USDT, bad)


def test_margin_and_quantity_conversions_are_inverses() -> None:
    qty = quantity_for_margin(1.1, 83.0, 24)
    assert margin_for_quantity(qty, 83.0, 24) == pytest.approx(1.1)


def test_roi_percent_scales_with_leverage_and_is_not_an_edge_measure() -> None:
    assert roi_percent(100.0, 101.09, 24) == pytest.approx(1.09 * 24)
    # Same price move, higher leverage, larger ROI%, identical dollar outcome.
    assert roi_percent(100.0, 101.09, 30) > roi_percent(100.0, 101.09, 24)
