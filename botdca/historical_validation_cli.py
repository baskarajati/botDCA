from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from botdca.backtest import IntrabarPath
from botdca.historical_profile import MINUTE_MS, load_or_fetch_candles
from botdca.historical_validation import validate_strategy_history
from botdca.marketdata import BybitKlineClient
from botdca.strategy import StrategyConfig
from botdca.trader_export import parse_trader_export


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate runtime DcaStrategy behavior against a private trader export"
    )
    parser.add_argument("--csv", required=True)
    parser.add_argument("--symbol", default="HYPEUSDT")
    parser.add_argument("--timezone", default="Europe/Rome")
    parser.add_argument("--baseline-leverage", type=int, default=24)
    parser.add_argument("--baseline-tp-percent", type=float, default=1.09)
    parser.add_argument("--base-margin", type=float, default=1.0)
    parser.add_argument("--fee-rate", type=float, default=0.00055)
    parser.add_argument("--match-window-seconds", type=float, default=300.0)
    parser.add_argument("--reentry-delay-seconds", type=float, default=48.0)
    parser.add_argument("--tp-change-threshold-percent", type=float, default=0.04)
    parser.add_argument("--close-time-tolerance-seconds", type=float, default=5.0)
    parser.add_argument("--close-price-tolerance-usdt", type=float, default=0.002)
    parser.add_argument(
        "--path",
        choices=["low-first", "high-first", "both"],
        default="both",
    )
    parser.add_argument("--cache-dir", default="~/.cache/botdca/marketdata")
    parser.add_argument("--refresh-market-data", action="store_true")
    parser.add_argument("--output", help="Optional path for aggregate JSON output")
    args = parser.parse_args()

    export = parse_trader_export(
        args.csv,
        timezone_name=args.timezone,
        close_time_tolerance_seconds=args.close_time_tolerance_seconds,
        close_price_tolerance_usdt=args.close_price_tolerance_usdt,
        expected_symbol=args.symbol,
        expected_side="Long",
    )
    start_ms = min(order.opened_ms for order in export.orders) // MINUTE_MS * MINUTE_MS
    end_ms = (
        max(order.closed_ms for order in export.orders) // MINUTE_MS * MINUTE_MS
        + int(args.match_window_seconds * 1000)
    )
    candles, cache_path, cache_hit = load_or_fetch_candles(
        BybitKlineClient(),
        symbol=args.symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        interval="1",
        cache_dir=args.cache_dir,
        refresh=args.refresh_market_data,
    )
    paths = (
        (IntrabarPath.LOW_FIRST, IntrabarPath.HIGH_FIRST)
        if args.path == "both"
        else (IntrabarPath(args.path),)
    )
    validation = validate_strategy_history(
        export,
        candles,
        baseline_config=StrategyConfig(
            symbol=args.symbol.upper(),
            leverage=args.baseline_leverage,
            base_margin_usdt=args.base_margin,
            tp_percent=args.baseline_tp_percent,
        ),
        intrabar_paths=paths,
        maximum_close_time_error_seconds=args.match_window_seconds,
        reentry_delay_seconds=args.reentry_delay_seconds,
        tp_change_threshold_percent=args.tp_change_threshold_percent,
        taker_fee_rate=args.fee_rate,
    )
    report = {
        "schema_version": 1,
        "report_type": "historical_strategy_validation",
        "private_source_committed": False,
        "market_data": {
            "provider": "Bybit V5 public linear klines",
            "symbol": args.symbol.upper(),
            "interval": "1",
            "cache_file": cache_path.name,
            "cache_hit": cache_hit,
            "candles": len(candles),
        },
        "dataset": {
            "source_rows": len(export.orders),
            "completed_cycles": len(export.cycles),
            "source_timezone": export.timezone_name,
        },
        "validation": validation,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")
    _print_summary(report, output=args.output)
    print(encoded)


def _print_summary(report: dict[str, object], *, output: str | None) -> None:
    validation = report["validation"]
    assert isinstance(validation, dict)
    scenarios = validation["scenarios"]
    assert isinstance(scenarios, list)
    for scenario in scenarios:
        anchored = scenario["anchored"]
        continuous = scenario["continuous"]
        print(
            f"{scenario['configuration']} / {scenario['intrabar_path']}: "
            f"anchored {anchored['matched_cycles']}/{anchored['actual_completed_cycles']}; "
            f"continuous {continuous['matched_cycles']}/{continuous['actual_completed_cycles']}",
            file=sys.stderr,
        )
    if output:
        print(f"Aggregate validation JSON written to {Path(output).expanduser()}", file=sys.stderr)


if __name__ == "__main__":
    main()
