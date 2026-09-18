from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from botdca.historical_profile import MINUTE_MS, load_or_fetch_candles
from botdca.marketdata import BybitKlineClient
from botdca.second_level_audit import audit_second_level_entry_uncertainty
from botdca.trader_export import parse_trader_export


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit one-minute strategy mismatches with archived public trades"
    )
    parser.add_argument("--csv", required=True)
    parser.add_argument("--fixed-entry-all-cycles", action="store_true")
    parser.add_argument("--trade-archive-dir", required=True)
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
    analysis = audit_second_level_entry_uncertainty(
        export,
        candles,
        args.trade_archive_dir,
        train_fraction=args.train_fraction,
        minimum_regime_cycles=args.minimum_regime_cycles,
        maximum_close_time_error_seconds=args.maximum_close_time_error_seconds,
        tp_change_threshold_percent=args.tp_change_threshold_percent,
        taker_fee_rate=args.fee_rate,
        fixed_entry_all_cycles=args.fixed_entry_all_cycles,
    )
    report = {
        "schema_version": 1,
        "report_type": "second_level_entry_uncertainty_audit",
        "private_source_committed": False,
        "raw_public_trades_committed": False,
        "market_data": {
            "minute_provider": "Bybit V5 public linear klines",
            "second_provider": "Bybit archived public trades",
            "symbol": args.symbol.upper(),
            "minute_cache_file": cache_path.name,
            "minute_cache_hit": cache_hit,
            "minute_candles": len(candles),
        },
        "analysis": analysis,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded + "\n", encoding="utf-8")
    conclusion = analysis["conclusion"]
    print(
        f"unresolved={analysis['unique_minute_level_unresolved_cycles']}; "
        f"recoverable={conclusion['resolution_recoverable_cycles']}; "
        f"model_mismatch={conclusion['model_mismatch_cycles']}; "
        f"data_gap={conclusion['data_gap_cycles']}",
        file=sys.stderr,
    )
    if args.output:
        print(f"Aggregate audit JSON written to {Path(args.output).expanduser()}", file=sys.stderr)
    print(encoded)


if __name__ == "__main__":
    main()
