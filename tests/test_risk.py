from decimal import Decimal

from botdca.instruments import InstrumentRules
from botdca.order_plan import build_resting_order_plan
from botdca.risk import RiskLimits, committed_margin_usdt, evaluate_next_dca
from botdca.strategy import DcaStrategy, StrategyConfig


def _rules() -> InstrumentRules:
    return InstrumentRules(
        symbol="HYPEUSDT",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.001"),
        min_order_qty=Decimal("0.01"),
        min_notional_value=Decimal(5),
        max_market_order_qty=Decimal(100),
    )


def _active_strategy() -> DcaStrategy:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    strategy.begin_cycle(80.0)
    return strategy


def test_first_dca_is_allowed_with_default_limits() -> None:
    strategy = _active_strategy()

    decision = evaluate_next_dca(strategy, RiskLimits())

    assert decision.allowed is True
    assert decision.next_dca_level == 1
    assert decision.projected_margin_usdt > committed_margin_usdt(strategy)


def test_margin_cap_blocks_next_dca_before_order_is_planned() -> None:
    strategy = _active_strategy()
    limits = RiskLimits(max_dca_level=8, max_strategy_margin_usdt=1.5)

    decision = evaluate_next_dca(strategy, limits)
    plan = build_resting_order_plan(strategy, _rules(), risk_limits=limits)

    assert decision.allowed is False
    assert "projected strategy margin" in (decision.reason or "")
    assert plan.next_dca is None
    assert plan.dca_blocked_reason == decision.reason
    assert plan.take_profit is not None


def test_max_dca_level_blocks_additional_dca() -> None:
    strategy = _active_strategy()
    strategy.apply_dca_fill(strategy.next_dca_trigger_price() or 79.0)
    limits = RiskLimits(max_dca_level=1, max_strategy_margin_usdt=100.0)

    decision = evaluate_next_dca(strategy, limits)

    assert decision.allowed is False
    assert decision.next_dca_level == 2
    assert "exceeds configured maximum" in (decision.reason or "")
