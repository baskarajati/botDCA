"""CLI for GreenSynergy-only historical profiling.

    botdca-profile-greensynergy \
        --csv /path/outside/git/bybit-greensynergy-past-trader-initiated-trades.csv \
        --workdir /path/outside/git/greensynergy-work \
        --symbol HYPEUSDT --symbol ONDOUSDT --symbol DOGEUSDT

The private CSV must stay outside the repository. Nothing this prints is an
execution rule or evidence that the reconstruction is validated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from botdca.greensynergy_profile import (
    portfolio_capital_expansion,
    profile_symbol_export,
    reconstruct_ladder_fit,
    simultaneous_deep_periods,
    split_export_by_symbol,
)
from botdca.strategy_version import GREENSYNERGY_RECONSTRUCTED_V1, get_strategy_version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Private GreenSynergy export, outside Git")
    parser.add_argument(
        "--workdir",
        required=True,
        help="Writable directory outside Git for the per-symbol partitions",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        default=None,
        help="Restrict to these symbols; repeat the flag. Defaults to every symbol found.",
    )
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument(
        "--leverage-filter",
        default="Cross 24.00x",
        help=(
            "Keep only rows with this margin/leverage label so startup and structural "
            "outliers do not define the profile. Pass an empty string to keep everything."
        ),
    )
    parser.add_argument("--deep-dca-level", type=int, default=5)
    parser.add_argument(
        "--strategy-version",
        default=GREENSYNERGY_RECONSTRUCTED_V1.version_id,
        help="Strategy version whose trigger ladder is scored against the data",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    workdir = Path(args.workdir)
    partitions = split_export_by_symbol(
        args.csv, workdir, leverage_filter=args.leverage_filter or None
    )
    if not partitions:
        print("No rows matched the requested filters.", file=sys.stderr)
        return 1

    wanted = [s.upper() for s in (args.symbol or sorted(partitions))]
    version = get_strategy_version(args.strategy_version)

    profiles = []
    report: dict = {
        "source": str(args.csv),
        "leverage_filter": args.leverage_filter or None,
        "strategy_version": version.describe(),
        "symbols": {},
        "disclaimer": (
            "Descriptive GreenSynergy research output. Not validated, not an execution "
            "rule, and never mixed with the Zuya dataset."
        ),
    }
    for symbol in wanted:
        path = partitions.get(symbol)
        if path is None:
            print(f"warning: {symbol} not present in the export", file=sys.stderr)
            continue
        profile = profile_symbol_export(path, symbol, timezone_name=args.timezone)
        profiles.append(profile)
        report["symbols"][symbol] = {
            **profile.describe(),
            "ladder_fit": reconstruct_ladder_fit(profile, version),
        }

    report["simultaneous_deep_periods"] = simultaneous_deep_periods(
        profiles, deep_dca_level=args.deep_dca_level
    )
    report["portfolio_capital_expansion"] = portfolio_capital_expansion(profiles)
    report["total_baskets"] = sum(len(profile.baskets) for profile in profiles)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    _print_text(report)
    return 0


def _print_text(report: dict) -> None:
    print("GreenSynergy historical profile (EXPERIMENTAL / RECONSTRUCTED)")
    print(f"  source: {report['source']}")
    print(f"  leverage filter: {report['leverage_filter']}")
    print(f"  strategy version: {report['strategy_version']['version_id']}")
    print(f"  total baskets: {report['total_baskets']}")
    for symbol, data in report["symbols"].items():
        if not data.get("basket_count"):
            continue
        print(f"\n  {symbol}")
        print(f"    rows / baskets       : {data['source_rows']} / {data['basket_count']}")
        print(f"    max observed DCA     : {data['max_observed_dca']}")
        print(f"    depth distribution   : {data['dca_depth_distribution']}")
        print(f"    take profit % median : {data['take_profit_percent']['median']:.4f}")
        print(f"    size multiplier med. : {data['size_multiplier']['median']:.4f}")
        gap = data["reentry_gap_seconds"]
        if gap["median"] is not None:
            print(
                f"    re-entry gap median  : {gap['median']:.0f}s "
                f"({gap['within_5_minutes_fraction'] * 100:.1f}% within 5 minutes)"
            )
        print(f"    initial margin median: {data['initial_margin_usdt']['median']:.4f} USDT")
        fit = data["ladder_fit"]
        error = fit["size_multiplier_relative_error_percent"]
        if error["median"] is not None:
            print(
                f"    qty multiplier vs {fit['expected_size_multiplier']}: "
                f"median |error| {error['median']:.2f}%, p90 {error['p90']:.2f}%"
            )
        drawdown = fit["implied_weighted_average_drawdown_percent"]
        if drawdown["median"] is not None:
            print(
                f"    implied avg drawdown : median {drawdown['median']:.3f}%, "
                f"max {drawdown['max']:.3f}% (trigger ladder not measurable here)"
            )
    deep = report["simultaneous_deep_periods"]
    print(
        f"\n  deep baskets (>=DCA{deep['deep_dca_level']}): {deep['deep_basket_count']}, "
        f"correlated overlapping pairs: {deep['overlapping_pair_count']}"
    )
    print(f"\n  {report['disclaimer']}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
