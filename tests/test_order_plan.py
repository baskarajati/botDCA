from decimal import Decimal

from botdca.instruments import InstrumentRules
from botdca.order_plan import build_resting_order_plan, initial_market_qty
from botdca.strategy import DcaStrategy, StrategyConfig


def rules() -> InstrumentRules:
    return InstrumentRules(
        symbol="HYPEUSDT",
        tick_size=Decimal("0.01"),
        qty_step=Decimal("0.001"),
        min_order_qty=Decimal("0.01"),
        min_notional_value=Decimal(5),
        max_market_order_qty=Decimal(100),
    )


def test_initial_market_qty_is_lot_size_safe() -> None:
    strategy = DcaStrategy(StrategyConfig())

    qty = initial_market_qty(strategy, rules(), market_price=80.0)

    assert qty == Decimal("0.300")


def test_resting_plan_rounds_tp_up_and_dca_down() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    cycle = strategy.begin_cycle(80.0)

    plan = build_resting_order_plan(strategy, rules())

    assert plan.take_profit.qty == Decimal("0.300")
    assert plan.take_profit.price == Decimal("80.88")
    assert plan.next_dca is not None
    assert plan.next_dca.price == Decimal("79.16")
    assert plan.next_dca.qty == Decimal("0.401")
    assert plan.take_profit.price > Decimal(str(cycle.tp_price))


def test_resting_plan_after_dca_uses_new_weighted_average() -> None:
    strategy = DcaStrategy(StrategyConfig())
    strategy.resume()
    strategy.begin_cycle(80.0)
    strategy.apply_dca_fill(79.16)

    plan = build_resting_order_plan(strategy, rules())

    assert plan.take_profit.qty > Decimal("0.300")
    assert plan.next_dca is not None
    assert plan.next_dca.price < Decimal("79.16")
