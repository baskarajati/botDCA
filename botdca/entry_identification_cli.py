from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from botdca.entry_identification import identify_entry_and_deep_dca_behavior
from botdca.historical_profile import MINUTE_MS, load_or_fetch_candles
from botdca.marketdata import BybitKlineClient
from botdca.trader_export import parse_trader_export


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Identify entry timing, re-entry scheduling, and sparse DCA9-10 evidence"
    )
    parser.add_argument("--csv", required=True)
    parser.add_argument("--symbol", default="HYPEUSDT")
    parser.add_argument("--timezone", default="Europe/Rome")
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--fixed-delay-seconds", type=float, default=48.0)
    parser.add_argument("--deep-dca-threshold", type=int, default=8)
    parser.add_argument("--long-cooldown-skipped-minutes", type=int, default=5)
    parser.add_argument("--price-tolerance-usdt", type=float, default=0.0)
    parser.add_argument("--tp-change-threshold-percent", type=float, default=0.04)
    parser.add_argument("--close-time-tolerance-seconds", type=float, default=5.0)
    parser.add_argument("--close-price-tolerance-usdt", type=float, default=0.002)
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
    end_ms = max(order.closed_ms for order in export.orders) // MINUTE_MS * MINUTE_MS + 300_000
    candles, cache_path, cache_hit = load_or_fetch_candles(
        BybitKlineClient(),
        symbol=args.symbol,
        start_ms=start_ms,
        end_ms=end_ms,
        interval="1",
        cache_dir=args.cache_dir,
        refresh=args.refresh_market_data,
    )
    analysis = identify_entry_and_deep_dca_behavior(
        export,
        candles,
        train_fraction=args.train_fraction,
        fixed_delay_seconds=args.fixed_delay_seconds,
        deep_dca_threshold=args.deep_dca_threshold,
        long_cooldown_skipped_minutes=args.long_cooldown_skipped_minutes,
        price_tolerance_usdt=args.price_tolerance_usdt,
        tp_change_threshold_percent=args.tp_change_threshold_percent,
    )
    report = {
        "schema_version": 1,
        "report_type": "entry_reentry_and_deep_dca_identification",
        "private_source_committed": False,
        "market_data": {
            "provider": "Bybit V5 public linear klines",
            "symbol": args.symbol.upper(),
            "interval": "1",
            "cache_file": cache_path.name,
            "cache_hit": cache_hit,
            "candles": len(candles),
        },
        "analysis": analysis,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")
    _print_summary(report, output=args.output)
    print(encoded)


def _print_summary(report: dict[str, object], *, output: str | None) -> None:
    analysis = report["analysis"]
    assert isinstance(analysis, dict)
    holdout = analysis["reentry_timing"]["holdout"]
    cooldown = analysis["deep_cycle_cooldown"]
    print(
        "holdout timing: "
        f"fixed48 median={holdout['fixed_delay_model_absolute_error_seconds']['median']:.1f}s; "
        f"next-minute scheduler median="
        f"{holdout['scheduler_model_absolute_error_seconds']['median']:.1f}s; "
        f"immediate={holdout['immediate_next_minute_percent']:.1f}%",
        file=sys.stderr,
    )
    print(
        f"deep cooldown: {cooldown['deep_long_cooldowns']}/"
        f"{cooldown['deep_transitions']} deep transitions vs "
        f"{cooldown['shallow_long_cooldowns']}/"
        f"{cooldown['shallow_transitions']} shallow transitions",
        file=sys.stderr,
    )
    for item in analysis["deep_dca_summary"]:
        print(
            f"regime {item['regime_index']} {item['leverage']:g}x DCA{item['level']}: "
            f"n={item['samples']} {item['interpretation']}",
            file=sys.stderr,
        )
    if output:
        print(f"Aggregate identification JSON written to {Path(output).expanduser()}", file=sys.stderr)


if __name__ == "__main__":
    main()
