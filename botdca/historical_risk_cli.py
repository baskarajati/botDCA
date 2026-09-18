from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from botdca.backtest import IntrabarPath
from botdca.historical_profile import MINUTE_MS, load_or_fetch_candles
from botdca.historical_risk import build_historical_risk_report
from botdca.marketdata import BybitKlineClient
from botdca.trader_export import parse_trader_export


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze economics, path risk, and approximate tail stress"
    )
    parser.add_argument("--csv", required=True)
    parser.add_argument("--calibration-report", required=True)
    parser.add_argument("--symbol", default="HYPEUSDT")
    parser.add_argument("--timezone", default="Europe/Rome")
    parser.add_argument("--fee-rate", type=float, default=0.00055)
    parser.add_argument("--maintenance-margin-rate", type=float, default=0.005)
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
    calibration_path = Path(args.calibration_report).expanduser()
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
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
    paths = (
        (IntrabarPath.LOW_FIRST, IntrabarPath.HIGH_FIRST)
        if args.path == "both"
        else (IntrabarPath(args.path),)
    )
    analysis = build_historical_risk_report(
        export,
        candles,
        calibration,
        intrabar_paths=paths,
        taker_fee_rate=args.fee_rate,
        maintenance_margin_rate=args.maintenance_margin_rate,
        tp_change_threshold_percent=args.tp_change_threshold_percent,
    )
    report = {
        "schema_version": 1,
        "report_type": "historical_economics_risk_and_stress",
        "private_source_committed": False,
        "market_data": {
            "provider": "Bybit V5 public linear klines",
            "symbol": args.symbol.upper(),
            "interval": "1",
            "cache_file": cache_path.name,
            "cache_hit": cache_hit,
            "candles": len(candles),
        },
        "calibration_report": calibration_path.name,
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
    coverage = analysis["coverage"]
    print(
        f"analyzed {coverage['analyzed_completed_cycles']}/"
        f"{coverage['export_completed_cycles']} observed cycles",
        file=sys.stderr,
    )
    for configuration, paths in analysis["comparisons"].items():
        for path, data in paths.items():
            aggregate = data["aggregate"]
            print(
                f"{configuration} / {path}: cycles={aggregate['completed_cycles']} "
                f"gross={aggregate['gross_realized_pnl_usdt']:.4f} "
                f"fees={aggregate['total_fees_paid_usdt']:.4f} "
                f"net_ex_funding={aggregate['net_realized_pnl_excluding_funding_usdt']:.4f} "
                f"max_drawdown={aggregate['maximum_regime_mark_to_market_drawdown_usdt']:.4f}",
                file=sys.stderr,
            )
    print(
        f"tail-risk hypothesis: {analysis['hypothesis_evaluation']['status']}",
        file=sys.stderr,
    )
    if output:
        print(f"Aggregate risk JSON written to {Path(output).expanduser()}", file=sys.stderr)


if __name__ == "__main__":
    main()
