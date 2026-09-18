"""Fit TP/quantity rounding on training only; replay previously seen failures."""

import argparse
import json
from dataclasses import replace
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from functools import reduce
from math import gcd

from botdca.backtest import IntrabarPath
from botdca.failure_trace import FailureTraceEngine
from botdca.historical_profile import split_regimes
from botdca.rounding_experiment import RoundedTpTraceEngine, fit_tp_rule
from botdca.second_level_audit import _cycle_key, _CycleWindow, load_archived_trade_seconds
from botdca.strategy import StrategyConfig
from botdca.trader_export import parse_trader_export


def quantity_diagnostic(training):
    config = StrategyConfig()
    quantities = [Decimal(str(o.order_qty)) for c in training for o in c.orders]
    scale = max(0, max(-q.as_tuple().exponent for q in quantities))
    divisor = 10**scale
    grid = Decimal(reduce(gcd, (int(q * divisor) for q in quantities))) / divisor
    pairs = [
        (
            Decimal(str(a.order_qty)),
            Decimal(str(b.order_qty)),
            Decimal(str(config.dca_steps[i].size_multiplier_from_previous)),
        )
        for c in training
        for i, (a, b) in enumerate(zip(c.orders, c.orders[1:]))
        if i < len(config.dca_steps)
    ]
    scores = []
    for mode in (None, ROUND_FLOOR, ROUND_HALF_UP, ROUND_CEILING):
        errors = []
        for previous, actual, multiplier in pairs:
            target = previous * multiplier
            if mode:
                target = (target / grid).to_integral_value(rounding=mode) * grid
            errors.append(float(abs(target - actual)))
        scores.append(
            {
                "mode": mode,
                "pairs": len(pairs),
                "mean_absolute_quantity_error": sum(errors) / len(errors),
                "exact_matches": sum(e < 1e-9 for e in errors),
            }
        )
    return {
        "observed_export_quantity_grid": str(grid),
        "scores": scores,
        "exchange_qty_step_proven": False,
        "replay_quantities_changed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--archive", required=True)
    args = parser.parse_args()
    from pathlib import Path

    audit = json.loads(Path(args.audit).read_text())["analysis"]
    failed = {
        int(datetime.fromisoformat(c["opened_utc"]).timestamp() * 1000)
        for c in audit["cycle_results"]
        if not any(a["matches"] for a in c["attempts"])
    }
    export = parse_trader_export(args.csv, timezone_name="Europe/Rome")
    selected, training_reports = [], []
    for index, regime in enumerate(split_regimes(list(export.cycles), threshold=0.04), 1):
        if len(regime) < 30:
            continue
        training = regime[: int(len(regime) * 0.7)]
        rule, candidates = fit_tp_rule(training)
        training_reports.append(
            {
                "regime": index,
                "selected_tp_rule": rule,
                "tp_candidates": candidates,
                "quantity": quantity_diagnostic(training),
            }
        )
        baseline = replace(
            StrategyConfig(),
            leverage=int(regime[0].leverage),
            tp_percent=round(candidates[0]["percent"], 6),
        )
        selected.extend((c, baseline, rule) for c in regime if c.opened_ms in failed)
    if len(selected) != len(failed):
        raise ValueError("could not map every prior failure")
    windows = [
        _CycleWindow(
            key=_cycle_key(c),
            start_ms=c.opened_ms,
            end_ms=c.closed_ms + 300000,
            order_timestamps_ms=tuple(o.opened_ms for o in c.orders),
            close_timestamp_ms=c.closed_ms,
        )
        for c, _, _ in selected
    ]
    candles, quality = load_archived_trade_seconds(
        Path(args.archive),
        symbol="HYPEUSDT",
        windows=windows,
    )
    rows = []
    for c, config, rule in selected:
        for path in IntrabarPath:
            for label in ("baseline", "training_selected_tp"):
                options = {
                    "intrabar_path": path,
                    "auto_reentry": False,
                    "maximum_completed_cycles": 1,
                }
                engine = (
                    FailureTraceEngine(config, **options)
                    if label == "baseline"
                    else RoundedTpTraceEngine(
                        replace(config, tp_percent=rule["percent"]), rule=rule, **options
                    )
                )
                result = engine.run(candles[_cycle_key(c)])
                completed = result.cycles[0] if result.cycles else None
                error = (completed.closed_ms - c.closed_ms) / 1000 if completed else None
                rows.append(
                    {
                        "opened_ms": c.opened_ms,
                        "label": label,
                        "path": path.value,
                        "observed_depth": c.dca_count,
                        "signed_close_error_seconds": error,
                        "modeled_depth": completed.dca_level
                        if completed
                        else result.open_cycle_dca_level,
                        "matches": completed is not None
                        and abs(error) <= 300
                        and completed.dca_level == c.dca_count,
                        "events": engine.events,
                    }
                )
    print(
        json.dumps(
            {
                "training": training_reports,
                "rows": rows,
                "source_quality": quality,
                "untouched_validation": False,
                "runtime_changed": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
