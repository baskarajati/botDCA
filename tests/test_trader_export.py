from datetime import UTC, datetime
from pathlib import Path

from botdca.backtest import BacktestResult, CycleReplay
from botdca.trader_export import (
    compare_replay_to_trader,
    load_trader_cycles,
    parse_trader_export,
)


def _write_export(path: Path) -> None:
    path.write_text(
        "index,page,row_on_page,position_symbol,position_side,margin_and_leverage,"
        "order_qty,roi_percent,entry_price,opened_on,closing_price,closed_on,followers\n"
        "0,1,1,HYPEUSDT,Long,Cross 24.00x,0.60 HYPE,+23.0%,78.000 USDT,"
        "2026-09-16 20:00:00,78.850 USDT,2026-09-16 21:00:00,1\n"
        "1,1,2,HYPEUSDT,Long,Cross 24.00x,0.80 HYPE,+23.4%,78.000 USDT,"
        "2026-09-16 20:30:00,78.850 USDT,2026-09-16 21:00:00,1\n"
        "2,1,3,HYPEUSDT,Long,Cross 24.00x,0.61 HYPE,+22.8%,79.000 USDT,"
        "2026-09-16 21:01:00,79.861 USDT,2026-09-16 22:00:00,1\n",
        encoding="utf-8",
    )


def test_load_trader_cycles_groups_entries_by_completed_cycle(tmp_path: Path) -> None:
    export = tmp_path / "trades.csv"
    _write_export(export)

    cycles = load_trader_cycles(export)

    assert len(cycles) == 2
    assert cycles[0].dca_count == 1
    assert cycles[0].total_qty == 1.4
    assert cycles[0].average_entry == 78.0
    assert cycles[0].closing_price == 78.85
    assert cycles[0].leverage == 24.0
    assert cycles[1].dca_count == 0


def test_compare_replay_matches_nearest_close_times(tmp_path: Path) -> None:
    export = tmp_path / "trades.csv"
    _write_export(export)
    actual = load_trader_cycles(export)

    simulated = [
        CycleReplay(
            cycle_id="a",
            opened_ms=actual[0].opened_ms,
            closed_ms=actual[0].closed_ms + 30_000,
            exit_price=78.80,
            gross_pnl_usdt=1.0,
            fees_usdt=0.1,
            net_pnl_usdt=0.9,
            dca_level=1,
            margin_deployed_usdt=2.0,
        ),
        CycleReplay(
            cycle_id="b",
            opened_ms=actual[1].opened_ms,
            closed_ms=actual[1].closed_ms - 20_000,
            exit_price=79.90,
            gross_pnl_usdt=1.0,
            fees_usdt=0.1,
            net_pnl_usdt=0.9,
            dca_level=0,
            margin_deployed_usdt=1.0,
        ),
    ]
    replay = BacktestResult(
        symbol="HYPEUSDT",
        intrabar_path="low-first",
        candles=100,
        completed_cycles=2,
        gross_realized_pnl_usdt=2.0,
        fees_paid_usdt=0.2,
        net_realized_pnl_usdt=1.8,
        max_dca_level=1,
        max_margin_deployed_usdt=2.0,
        max_mark_to_market_drawdown_usdt=1.0,
        final_unrealized_pnl_usdt=0.0,
        open_cycle_dca_level=0,
        cycles=simulated,
    )

    result = compare_replay_to_trader(replay, actual, maximum_close_time_error_seconds=60)

    assert result.matched_cycles == 2
    assert result.match_rate_percent == 100.0
    assert result.exact_dca_match_percent == 100.0
    assert result.median_close_time_error_seconds == 25.0


def test_parse_trader_export_merges_close_fragments_and_converts_timezone(
    tmp_path: Path,
) -> None:
    export = tmp_path / "fragmented.csv"
    export.write_text(
        "position_symbol,position_side,margin_and_leverage,order_qty,roi_percent,"
        "entry_price,opened_on,closing_price,closed_on,followers\n"
        "HYPEUSDT,Long,Cross 24.00x,1.00 HYPE,+20%,99.000 USDT,"
        "2026-09-16 20:00:00,100.000 USDT,2026-09-16 21:00:00,1\n"
        "HYPEUSDT,Long,Cross 24.00x,1.40 HYPE,+20%,99.000 USDT,"
        "2026-09-16 20:30:00,100.001 USDT,2026-09-16 21:00:02,1\n"
        "HYPEUSDT,Long,Cross 24.00x,1.00 HYPE,+20%,100.000 USDT,"
        "2026-09-16 21:01:00,101.000 USDT,2026-09-16 22:00:00,1\n",
        encoding="utf-8",
    )

    parsed = parse_trader_export(
        export,
        timezone_name="Europe/Rome",
        expected_symbol="HYPEUSDT",
        expected_side="Long",
    )

    assert parsed.grouping.exact_key_groups == 3
    assert parsed.grouping.grouped_cycles == 2
    assert parsed.grouping.close_fragment_groups_merged == 1
    assert parsed.grouping.overlapping_cycles == 0
    assert parsed.cycles[0].dca_count == 1
    assert parsed.cycles[0].close_fragment_count == 2
    assert parsed.cycles[0].closed_ms == int(
        datetime(2026, 9, 16, 19, 0, 2, tzinfo=UTC).timestamp() * 1000
    )


def test_parse_trader_export_rejects_wrong_symbol(tmp_path: Path) -> None:
    export = tmp_path / "wrong-symbol.csv"
    export.write_text(
        "position_symbol,position_side,margin_and_leverage,order_qty,roi_percent,"
        "entry_price,opened_on,closing_price,closed_on,followers\n"
        "BTCUSDT,Long,Cross 24.00x,1 BTC,+20%,100 USDT,"
        "2026-09-16 20:00:00,101 USDT,2026-09-16 21:00:00,1\n",
        encoding="utf-8",
    )

    try:
        parse_trader_export(export, expected_symbol="HYPEUSDT")
    except ValueError as exc:
        assert "expected HYPEUSDT" in str(exc)
    else:
        raise AssertionError("wrong symbol should be rejected")
