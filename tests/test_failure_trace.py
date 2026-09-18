from botdca.backtest import Candle, ReplayEngine
from botdca.failure_trace import FailureTraceEngine
from botdca.strategy import StrategyConfig


def test_observer_preserves_replay_and_exposes_missed_open_gap():
    config = StrategyConfig(tp_percent=1)
    candles = [Candle(0, 100, 100, 100, 100), Candle(1000, 102, 102, 102, 102)]
    traced = FailureTraceEngine(config, auto_reentry=False)
    result = traced.run(candles)
    baseline = ReplayEngine(config, auto_reentry=False).run(candles)
    assert result.completed_cycles == baseline.completed_cycles == 0
    assert result.open_cycle_dca_level == baseline.open_cycle_dca_level == 0
    assert traced.open_gap_crossings == [
        {
            "kind": "tp",
            "timestamp_ms": 1000,
            "previous_close": 100,
            "open": 102,
            "threshold": 101,
            "entire_candle_beyond_threshold": True,
        }
    ]


def test_observer_records_normal_fills_without_changing_them():
    config = StrategyConfig(tp_percent=1)
    candles = [Candle(0, 100, 100, 100, 100), Candle(1000, 100, 102, 98, 101)]
    traced = FailureTraceEngine(config, auto_reentry=False)
    result = traced.run(candles)
    baseline = ReplayEngine(config, auto_reentry=False).run(candles)
    assert result.completed_cycles == baseline.completed_cycles == 1
    assert result.cycles[0].fill_prices == baseline.cycles[0].fill_prices
    assert result.cycles[0].exit_price == baseline.cycles[0].exit_price
    assert result.net_realized_pnl_usdt == baseline.net_realized_pnl_usdt
    assert [e["kind"] for e in traced.events] == ["dca", "tp"]
    assert traced.open_gap_crossings == []
