"""Freeze training price/timing proxy boxes, then audit prior failures."""

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from statistics import median

from botdca.backtest import IntrabarPath, ReplayEngine
from botdca.entry_uncertainty import learn_entry_bounds, training_sample
from botdca.historical_profile import split_regimes
from botdca.second_level_audit import (
    _cycle_key,
    _CycleWindow,
    _tp_percent,
    _with_first_open,
    load_archived_trade_seconds,
)
from botdca.strategy import StrategyConfig
from botdca.trader_export import parse_trader_export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    audit = json.loads(Path(args.audit).read_text())["analysis"]
    failures = {
        int(datetime.fromisoformat(c["opened_utc"]).timestamp() * 1000)
        for c in audit["cycle_results"]
        if not any(a["matches"] for a in c["attempts"])
    }
    export = parse_trader_export(args.csv, timezone_name="Europe/Rome")
    groups, windows = [], []
    for index, regime in enumerate(split_regimes(list(export.cycles), threshold=0.04), 1):
        if len(regime) < 30:
            continue
        sample = training_sample(regime)
        training = regime[: int(len(regime) * 0.7)]
        config = replace(
            StrategyConfig(),
            leverage=int(regime[0].leverage),
            tp_percent=round(median(_tp_percent(c) for c in training), 6),
        )
        selected = [c for c in regime if c.opened_ms in failures]
        groups.append((index, sample, selected, config))
        for c in sample:
            windows.append(
                _CycleWindow(
                    key="train-" + _cycle_key(c),
                    start_ms=c.opened_ms - 60000,
                    end_ms=c.opened_ms + 60000,
                    order_timestamps_ms=(c.opened_ms,),
                    close_timestamp_ms=c.opened_ms,
                )
            )
        for c in selected:
            windows.append(
                _CycleWindow(
                    key=_cycle_key(c),
                    start_ms=c.opened_ms - 60000,
                    end_ms=c.closed_ms + 300000,
                    order_timestamps_ms=tuple(o.opened_ms for o in c.orders),
                    close_timestamp_ms=c.closed_ms,
                )
            )
    if sum(len(g[2]) for g in groups) != len(failures):
        raise ValueError("not all failures mapped")
    print("Scanning training and failure archive windows", file=sys.stderr, flush=True)
    candles, quality = load_archived_trade_seconds(args.archive, symbol="HYPEUSDT", windows=windows)
    bound_reports, results = [], []
    for index, sample, selected, config in groups:
        bounds = learn_entry_bounds(
            sample, {c.opened_ms: candles["train-" + _cycle_key(c)] for c in sample}
        )
        bound_reports.append({"regime": index, **bounds})
        print(
            f"Frozen regime {index}: {bounds['relative_price_bound']:.8f} price box, "
            f"{bounds['timing_proxy_bound_ms']} ms timing proxy",
            file=sys.stderr,
            flush=True,
        )
        for c in selected:
            all_candles = candles[_cycle_key(c)]
            at_clock = [bar for bar in all_candles if bar.start_ms >= c.opened_ms]
            if not at_clock:
                raise ValueError("missing failure entry coverage")
            candidates = []
            for fraction in (-1, -0.5, 0, 0.5, 1):
                price = at_clock[0].open * (1 + fraction * bounds["relative_price_bound"])
                candidates.append(("price_box", fraction, _with_first_open(at_clock, price)))
            timing = bounds["timing_proxy_bound_ms"]
            if timing is not None:
                for offset in sorted({-timing, 0, timing}):
                    shifted = [bar for bar in all_candles if bar.start_ms >= c.opened_ms + offset]
                    if not shifted:
                        raise ValueError("missing shifted failure prices")
                    candidates.append(("timing_proxy", offset, shifted))
            attempts = []
            for kind, offset, bars in candidates:
                public_bar = at_clock[0] if kind == "price_box" else bars[0]
                for path in IntrabarPath:
                    replay = ReplayEngine(
                        config, intrabar_path=path, auto_reentry=False, maximum_completed_cycles=1
                    ).run(bars)
                    completed = replay.cycles[0] if replay.cycles else None
                    error = (completed.closed_ms - c.closed_ms) / 1000 if completed else None
                    attempts.append(
                        {
                            "kind": kind,
                            "offset": offset,
                            "path": path.value,
                            "entry_price": bars[0].open,
                            "entry_timestamp_ms": bars[0].start_ms,
                            "seed_within_selected_second_public_range": public_bar.low
                            <= bars[0].open
                            <= public_bar.high,
                            "seed_consistent_with_exported_depth_zero_average": abs(
                                bars[0].open - c.average_entry
                            )
                            <= 0.00050001
                            if c.dca_count == 0
                            else None,
                            "signed_close_error_seconds": error,
                            "modeled_depth": completed.dca_level
                            if completed
                            else replay.open_cycle_dca_level,
                            "matches": completed is not None
                            and abs(error) <= 300
                            and completed.dca_level == c.dca_count,
                        }
                    )
            results.append(
                {
                    "regime": index,
                    "opened_ms": c.opened_ms,
                    "observed_depth": c.dca_count,
                    "exported_depth_zero_average": c.average_entry if c.dca_count == 0 else None,
                    "entry_second_public_range": {"low": at_clock[0].low, "high": at_clock[0].high},
                    "attempts": attempts,
                }
            )
            print(
                f"Traced failure {c.opened_ms}: {sum(a['matches'] for a in attempts)} matching grid attempts",
                file=sys.stderr,
                flush=True,
            )
    print(
        json.dumps(
            {
                "training_bounds": bound_reports,
                "results": results,
                "source_quality": quality,
                "range_supported_recoveries": {
                    path.value: sum(
                        any(
                            a["matches"]
                            and a["path"] == path.value
                            and a["seed_within_selected_second_public_range"]
                            for a in r["attempts"]
                        )
                        for r in results
                    )
                    for path in IntrabarPath
                },
                "runtime_changed": False,
                "untouched_validation": False,
                "feasibility_not_strategy_score": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
