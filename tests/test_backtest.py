import pytest

from botdca.backtest import Candle, IntrabarPath, ReplayEngine
from botdca.marketdata import BybitKlineClient
from botdca.strategy import StrategyConfig


def test_replay_completes_tp_cycle() -> None:
    candles = [Candle(start_ms=0, open=100.0, high=101.09, low=100.0, close=101.09)]
    result = ReplayEngine(
        StrategyConfig(), intrabar_path=IntrabarPath.LOW_FIRST
    ).run(candles)

    assert result.completed_cycles == 1
    assert result.gross_realized_pnl_usdt > 0
    assert result.fees_paid_usdt > 0
    assert result.net_realized_pnl_usdt < result.gross_realized_pnl_usdt
    assert result.open_cycle_dca_level == 0


def test_replay_dca_then_take_profit() -> None:
    candles = [Candle(start_ms=0, open=100.0, high=101.0, low=98.5, close=100.7)]
    result = ReplayEngine(
        StrategyConfig(), intrabar_path=IntrabarPath.LOW_FIRST
    ).run(candles)

    assert result.completed_cycles >= 1
    assert result.cycles[0].dca_level == 1
    assert result.max_dca_level >= 1
    assert result.max_margin_deployed_usdt > 1.0


def test_large_drop_can_cross_multiple_dca_levels_in_one_candle() -> None:
    candles = [Candle(start_ms=0, open=100.0, high=100.0, low=95.0, close=95.0)]
    result = ReplayEngine(
        StrategyConfig(), intrabar_path=IntrabarPath.HIGH_FIRST
    ).run(candles)

    assert result.completed_cycles == 0
    assert result.max_dca_level >= 2
    assert result.final_unrealized_pnl_usdt < 0
    assert result.max_mark_to_market_drawdown_usdt > 0


def test_intrabar_ordering_changes_result() -> None:
    candles = [Candle(start_ms=0, open=100.0, high=101.3, low=98.5, close=100.5)]
    low_first = ReplayEngine(
        StrategyConfig(), intrabar_path=IntrabarPath.LOW_FIRST
    ).run(candles)
    high_first = ReplayEngine(
        StrategyConfig(), intrabar_path=IntrabarPath.HIGH_FIRST
    ).run(candles)

    assert low_first.net_realized_pnl_usdt != high_first.net_realized_pnl_usdt


def test_replay_can_stop_after_one_cycle_without_reentry() -> None:
    candles = [Candle(start_ms=0, open=100, high=103, low=100, close=103)]

    result = ReplayEngine(
        StrategyConfig(tp_percent=1.0),
        auto_reentry=False,
        maximum_completed_cycles=1,
    ).run(candles)

    assert result.completed_cycles == 1
    assert result.open_cycle_dca_level is None


def test_reentry_delay_waits_for_an_eligible_candle() -> None:
    candles = [
        Candle(start_ms=0, open=100, high=101, low=100, close=101),
        Candle(start_ms=60_000, open=100, high=101, low=100, close=101),
    ]

    result = ReplayEngine(
        StrategyConfig(tp_percent=1.0),
        reentry_delay_seconds=48,
    ).run(candles)

    assert result.completed_cycles == 2
    assert [cycle.opened_ms for cycle in result.cycles] == [0, 60_000]


def test_replay_records_fee_excursion_and_recovery_telemetry() -> None:
    candles = [
        Candle(start_ms=0, open=100, high=100, low=99, close=99),
        Candle(start_ms=60_000, open=99, high=101.2, low=99, close=101.1),
    ]

    result = ReplayEngine(
        StrategyConfig(tp_percent=1.09),
        auto_reentry=False,
        maximum_completed_cycles=1,
    ).run(candles)

    cycle = result.cycles[0]
    assert cycle.entry_fees_usdt > 0
    assert cycle.exit_fee_usdt > 0
    assert cycle.fees_usdt == cycle.entry_fees_usdt + cycle.exit_fee_usdt
    assert cycle.maximum_adverse_excursion_usdt > 0
    assert cycle.maximum_favorable_excursion_usdt > 0
    assert cycle.maximum_adverse_excursion_percent == pytest.approx(1.0)
    assert cycle.duration_seconds == 60.0
    assert result.peak_position_notional_usdt > 24.0
    assert result.worst_floating_pnl_usdt < 0
    assert result.longest_open_cycle_seconds == 60.0
    assert result.recovery_durations_seconds == (60.0,)
    assert result.longest_underwater_seconds == 60.0
    assert result.funding_paid_usdt is None
    assert result.net_pnl_includes_funding is False
    assert result.realized_cycle_fees_usdt == cycle.fees_usdt
    assert result.open_cycle_entry_fees_paid_usdt == 0.0


def test_replay_accepts_timestamped_funding_model_without_inventing_rates() -> None:
    class OnePayment:
        def payment_usdt(
            self,
            *,
            timestamp_ms: int,
            mark_price: float,
            position_quantity: float,
        ) -> float:
            del mark_price, position_quantity
            return 0.25 if timestamp_ms == 60_000 else 0.0

    result = ReplayEngine(
        StrategyConfig(tp_percent=1.0),
        funding_model=OnePayment(),
        auto_reentry=False,
        maximum_completed_cycles=1,
    ).run(
        [
            Candle(start_ms=0, open=100, high=100, low=100, close=100),
            Candle(start_ms=60_000, open=100, high=101, low=100, close=101),
        ]
    )

    assert result.funding_paid_usdt == 0.25
    assert result.net_realized_after_funding_usdt == pytest.approx(
        result.net_realized_pnl_usdt - 0.25
    )
    assert result.net_pnl_includes_funding is False


def test_bybit_rows_are_parsed_and_sorted() -> None:
    rows = [
        ["60000", "101", "102", "100", "101.5", "1", "1"],
        ["0", "100", "101", "99", "100.5", "1", "1"],
    ]
    candles = BybitKlineClient._parse_rows(rows, start_ms=0, end_ms=60000)

    assert [candle.start_ms for candle in candles] == [0, 60000]
    assert candles[0].open == 100.0
