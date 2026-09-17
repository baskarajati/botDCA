from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from botdca.backtest import IntrabarPath, ReplayEngine
from botdca.marketdata import BybitKlineClient
from botdca.strategy import StrategyConfig
from botdca.trader_export import compare_replay_to_trader, load_trader_cycles


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare the reconstructed strategy with a Bybit trader export"
    )
    parser.add_argument("--csv", required=True, help="Path to the local Bybit trader CSV")
    parser.add_argument("--symbol", default="HYPEUSDT")
    parser.add_argument("--interval", default="1")
    parser.add_argument("--leverage", type=int, default=24)
    parser.add_argument("--base-margin", type=float, default=1.0)
    parser.add_argument("--tp-percent", type=float, default=1.09)
    parser.add_argument("--fee-rate", type=float, default=0.00055)
    parser.add_argument(
        "--timezone",
        default="Europe/Rome",
        help="IANA timezone of timestamps in the trader export",
    )
    parser.add_argument(
        "--timezone-offset-minutes",
        type=int,
        help="Legacy fixed offset; overrides --timezone when supplied",
    )
    parser.add_argument("--match-window-seconds", type=float, default=300.0)
    parser.add_argument(
        "--path",
        choices=["low-first", "high-first", "both"],
        default="both",
    )
    args = parser.parse_args()

    actual = load_trader_cycles(
        args.csv,
        timezone_offset_minutes=args.timezone_offset_minutes or 0,
        timezone_name=args.timezone if args.timezone_offset_minutes is None else None,
    )
    actual = [cycle for cycle in actual if abs(cycle.leverage - args.leverage) < 1e-9]
    if not actual:
        raise SystemExit("no trader cycles matched the requested leverage")

    # Give the replay enough context to enter before the first observed close.
    start_ms = min(cycle.opened_ms for cycle in actual) - 60_000
    end_ms = max(cycle.closed_ms for cycle in actual) + 60_000
    config = StrategyConfig(
        symbol=args.symbol.upper(),
        leverage=args.leverage,
        base_margin_usdt=args.base_margin,
        tp_percent=args.tp_percent,
    )
    candles = BybitKlineClient().fetch_linear_candles(
        symbol=config.symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        interval=args.interval,
    )
    if not candles:
        raise SystemExit("Bybit returned no candles for the trader-export period")

    paths = (
        [IntrabarPath.LOW_FIRST, IntrabarPath.HIGH_FIRST]
        if args.path == "both"
        else [IntrabarPath(args.path)]
    )

    output: list[dict[str, object]] = []
    for path in paths:
        replay = ReplayEngine(config, intrabar_path=path, taker_fee_rate=args.fee_rate).run(candles)
        comparison = compare_replay_to_trader(
            replay,
            actual,
            maximum_close_time_error_seconds=args.match_window_seconds,
        )
        output.append(
            {
                "intrabar_path": path.value,
                "replay": {
                    "completed_cycles": replay.completed_cycles,
                    "net_realized_pnl_usdt": replay.net_realized_pnl_usdt,
                    "max_dca_level": replay.max_dca_level,
                    "max_margin_deployed_usdt": replay.max_margin_deployed_usdt,
                    "max_mark_to_market_drawdown_usdt": replay.max_mark_to_market_drawdown_usdt,
                    "final_unrealized_pnl_usdt": replay.final_unrealized_pnl_usdt,
                },
                "comparison": asdict(comparison),
            }
        )

    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
