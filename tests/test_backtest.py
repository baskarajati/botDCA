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
    candles = [Candle(start_ms=0, open=100.0, high=100.0, low=95.0, close=95.5)]
    result = ReplayEngine(
        StrategyConfig(), intrabar_path=IntrabarPath.LOW_FIRST
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


def test_bybit_rows_are_parsed_and_sorted() -> None:
    rows = [
        ["60000", "101", "102", "100", "101.5", "1", "1"],
        ["0", "100", "101", "99", "100.5", "1", "1"],
    ]
    candles = BybitKlineClient._parse_rows(rows, start_ms=0, end_ms=60000)

    assert [candle.start_ms for candle in candles] == [0, 60000]
    assert candles[0].open == 100.0
