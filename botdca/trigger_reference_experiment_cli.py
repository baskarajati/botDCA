from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from botdca.historical_profile import MINUTE_MS, load_or_fetch_candles
from botdca.marketdata import BybitKlineClient
from botdca.trader_export import parse_trader_export
from botdca.trigger_reference_experiment import compare_dca_trigger_references


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare DCA weighted-average, previous-fill, and initial-entry references"
    )
    parser.add_argument("--csv", required=True)
    parser.add_argument("--symbol", default="HYPEUSDT")
    parser.add_argument("--timezone", default="Europe/Rome")
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--minimum-regime-cycles", type=int, default=30)
    parser.add_argument("--maximum-close-time-error-seconds", type=float, default=300.0)
    parser.add_argument("--tp-change-threshold-percent", type=float, default=0.04)
    parser.add_argument("--fee-rate", type=float, default=0.00055)
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
    analysis = compare_dca_trigger_references(
        export,
        candles,
        train_fraction=args.train_fraction,
        minimum_regime_cycles=args.minimum_regime_cycles,
        maximum_close_time_error_seconds=args.maximum_close_time_error_seconds,
        tp_change_threshold_percent=args.tp_change_threshold_percent,
        taker_fee_rate=args.fee_rate,
    )
    report = {
        "schema_version": 1,
        "report_type": "dca_trigger_reference_experiment",
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
    _print_summary(analysis, output=args.output)
    print(encoded)


def _print_summary(analysis: dict[str, object], *, output: str | None) -> None:
    conclusion = analysis["conclusion"]
    assert isinstance(conclusion, dict)
    print(
        f"best observed reference: {conclusion['best_observed_reference']}; "
        f"status={conclusion['status']}; "
        f"all targets passed={conclusion['all_holdout_targets_passed']}",
        file=sys.stderr,
    )
    models = analysis["aggregate_models"]
    assert isinstance(models, list)
    for model in models:
        assert isinstance(model, dict)
        low = model["holdout_by_path"]["low-first"]
        high = model["holdout_by_path"]["high-first"]
        print(
            f"{model['reference']}: low-first {low['matched_cycles']}/"
            f"{low['actual_completed_cycles']}; high-first {high['matched_cycles']}/"
            f"{high['actual_completed_cycles']}; executable="
            f"{model['normalized_full_ladder_executable_on_descent']}",
            file=sys.stderr,
        )
    if output:
        print(f"Aggregate experiment JSON written to {Path(output).expanduser()}", file=sys.stderr)


if __name__ == "__main__":
    main()
