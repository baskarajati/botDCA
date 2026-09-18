"""Trace failures selected from an existing fixed-entry audit, without tuning."""

import argparse
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from statistics import median

from botdca.backtest import IntrabarPath
from botdca.failure_trace import FailureTraceEngine
from botdca.historical_profile import split_regimes
from botdca.second_level_audit import (
    _cycle_key,
    _CycleWindow,
    _tp_percent,
    load_archived_trade_seconds,
)
from botdca.strategy import StrategyConfig
from botdca.trader_export import parse_trader_export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--timezone", default="Europe/Rome")
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())["analysis"]
    failed = {
        int(datetime.fromisoformat(c["opened_utc"]).timestamp() * 1000)
        for c in audit["cycle_results"]
        if not any(a["matches"] for a in c["attempts"])
    }
    export = parse_trader_export(args.csv, timezone_name=args.timezone)
    selected = []
    for regime in split_regimes(list(export.cycles), threshold=0.04):
        if len(regime) < 30:
            continue
        split = int(len(regime) * 0.7)
        config = replace(
            StrategyConfig(),
            leverage=int(regime[0].leverage),
            tp_percent=round(median(_tp_percent(c) for c in regime[:split]), 6),
        )
        selected.extend((c, config) for c in regime if c.opened_ms in failed)
    if len(selected) != len(failed):
        raise ValueError("not all selected failures belong to eligible regimes")
    windows = [
        _CycleWindow(
            key=_cycle_key(c),
            start_ms=c.opened_ms,
            end_ms=c.closed_ms + 300000,
            order_timestamps_ms=tuple(o.opened_ms for o in c.orders),
            close_timestamp_ms=c.closed_ms,
        )
        for c, _ in selected
    ]
    candles, quality = load_archived_trade_seconds(
        args.archive,
        symbol="HYPEUSDT",
        windows=windows,
    )
    rows = []
    for c, config in selected:
        for path in IntrabarPath:
            engine = FailureTraceEngine(
                config,
                intrabar_path=path,
                auto_reentry=False,
                maximum_completed_cycles=1,
            )
            result = engine.run(candles[_cycle_key(c)])
            rows.append(
                {
                    "opened_ms": c.opened_ms,
                    "closed_ms": c.closed_ms,
                    "path": path.value,
                    "observed_depth": c.dca_count,
                    "observed_average": c.average_entry,
                    "observed_close": c.closing_price,
                    "tp_percent": config.tp_percent,
                    "entry_price": candles[_cycle_key(c)][0].open,
                    "observed_order_times": [o.opened_ms for o in c.orders],
                    "events": engine.events,
                    "open_gap_crossings": engine.open_gap_crossings,
                    "completed": result.completed_cycles,
                    "open_depth": result.open_cycle_dca_level,
                }
            )
    print(json.dumps({"rows": rows, "quality": quality}, indent=2))


if __name__ == "__main__":
    main()
