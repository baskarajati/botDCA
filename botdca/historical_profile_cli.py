from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from botdca.historical_profile import MINUTE_MS, build_historical_profile, load_or_fetch_candles
from botdca.marketdata import BybitKlineClient
from botdca.trader_export import parse_trader_export


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Profile a private Bybit trader export against public one-minute market data"
    )
    parser.add_argument("--csv", required=True, help="Path to the private trader export")
    parser.add_argument("--symbol", default="HYPEUSDT")
    parser.add_argument("--timezone", default="Europe/Rome", help="IANA timezone of CSV timestamps")
    parser.add_argument("--interval", default="1", choices=["1"])
    parser.add_argument("--close-time-tolerance-seconds", type=float, default=5.0)
    parser.add_argument("--close-price-tolerance-usdt", type=float, default=0.002)
    parser.add_argument("--tp-change-threshold-percent", type=float, default=0.04)
    parser.add_argument(
        "--cache-dir",
        default="~/.cache/botdca/marketdata",
        help="Directory for public candle caches",
    )
    parser.add_argument("--refresh-market-data", action="store_true")
    parser.add_argument(
        "--skip-market-data",
        action="store_true",
        help="Profile the export without downloading or validating candles",
    )
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

    candles = None
    market_data: dict[str, object] = {"included": False}
    if not args.skip_market_data:
        start_ms = min(order.opened_ms for order in export.orders) // MINUTE_MS * MINUTE_MS
        end_ms = max(order.closed_ms for order in export.orders) // MINUTE_MS * MINUTE_MS
        candles, cache_path, cache_hit = load_or_fetch_candles(
            BybitKlineClient(),
            symbol=args.symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval=args.interval,
            cache_dir=args.cache_dir,
            refresh=args.refresh_market_data,
        )
        market_data = {
            "included": True,
            "provider": "Bybit V5 public linear klines",
            "symbol": args.symbol.upper(),
            "interval": args.interval,
            "cache_file": cache_path.name,
            "cache_hit": cache_hit,
        }

    report = {
        "schema_version": 1,
        "report_type": "observed_trader_behavior",
        "private_source_committed": False,
        "market_data": market_data,
        "profile": build_historical_profile(
            export,
            candles=candles,
            tp_change_threshold_percent=args.tp_change_threshold_percent,
        ),
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")

    _print_summary(report, output=args.output)
    print(encoded)


def _print_summary(report: dict[str, object], *, output: str | None) -> None:
    profile = report["profile"]
    assert isinstance(profile, dict)
    dataset = profile["dataset"]
    assert isinstance(dataset, dict)
    grouping = dataset["grouping"]
    assert isinstance(grouping, dict)
    depth = profile["dca_depth"]
    assert isinstance(depth, dict)
    print(
        "Trader export profile: "
        f"{dataset['source_rows']} rows -> {dataset['completed_cycles']} cycles; "
        f"merged {grouping['close_fragment_groups_merged']} close-fragment groups; "
        f"max DCA depth {depth['maximum_observed']}.",
        file=sys.stderr,
    )
    if "market_alignment" in profile:
        alignment = profile["market_alignment"]
        assert isinstance(alignment, dict)
        print(
            "Market alignment: "
            f"{alignment['weighted_average_feasible_cycles']}/{dataset['completed_cycles']} "
            "weighted averages feasible; "
            f"{alignment['closing_price_in_range_cycles']}/{dataset['completed_cycles']} "
            "closing prices inside their one-minute ranges.",
            file=sys.stderr,
        )
    if output:
        print(f"Aggregate JSON report written to {Path(output).expanduser()}", file=sys.stderr)


if __name__ == "__main__":
    main()
