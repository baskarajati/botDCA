from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import asdict
from statistics import median
from typing import Any

from botdca.backtest import BacktestResult, Candle, IntrabarPath, ReplayEngine
from botdca.domain import DcaStep
from botdca.historical_profile import MINUTE_MS, split_regimes
from botdca.strategy import DcaStrategy, StrategyConfig
from botdca.trader_export import TraderCycle, TraderExportData


def build_ladder_stress_analysis(
    config: StrategyConfig,
    *,
    anchor_price: float = 100.0,
    taker_fee_rate: float = 0.00055,
    maintenance_margin_rate: float = 0.005,
    additional_drop_percents: tuple[float, ...] = (5.0, 10.0, 20.0),
) -> dict[str, Any]:
    """Exhaust the runtime ladder, then apply explicitly approximate shocks."""

    if anchor_price <= 0:
        raise ValueError("anchor_price must be positive")
    if taker_fee_rate < 0:
        raise ValueError("taker_fee_rate cannot be negative")
    if maintenance_margin_rate < 0:
        raise ValueError("maintenance_margin_rate cannot be negative")
    if not additional_drop_percents or any(
        drop <= 0 or drop >= 100 for drop in additional_drop_percents
    ):
        raise ValueError("additional stress drops must be between zero and 100")

    strategy = DcaStrategy(config)
    strategy.resume()
    cycle = strategy.begin_cycle(anchor_price)
    while True:
        trigger = strategy.next_dca_trigger_price()
        quantity = strategy.next_dca_qty()
        if trigger is None or quantity is None:
            break
        strategy.apply_dca_fill(trigger, quantity)

    average_entry = cycle.average_entry
    if average_entry is None:
        raise RuntimeError("stress ladder produced no position")
    total_quantity = cycle.total_qty
    entry_notional = sum(fill.price * fill.qty for fill in cycle.fills)
    initial_notional = cycle.fills[0].price * cycle.fills[0].qty
    entry_fees = entry_notional * taker_fee_rate
    margin_deployed = entry_notional / config.leverage
    final_fill_price = cycle.fills[-1].price
    scenarios: list[dict[str, Any]] = []
    for drop in additional_drop_percents:
        mark_price = final_fill_price * (1 - drop / 100)
        mark_notional = mark_price * total_quantity
        floating_pnl = sum((mark_price - fill.price) * fill.qty for fill in cycle.fills)
        estimated_close_fee = mark_notional * taker_fee_rate
        maintenance_buffer = mark_notional * maintenance_margin_rate
        reserve_proxy = (
            margin_deployed
            + max(0.0, -floating_pnl)
            + entry_fees
            + estimated_close_fee
            + maintenance_buffer
        )
        scenarios.append(
            {
                "additional_drop_from_final_dca_percent": drop,
                "mark_price": mark_price,
                "position_notional_usdt": mark_notional,
                "floating_pnl_usdt": floating_pnl,
                "floating_loss_usdt": max(0.0, -floating_pnl),
                "estimated_close_fee_usdt": estimated_close_fee,
                "maintenance_buffer_usdt": maintenance_buffer,
                "approximate_account_equity_reserve_usdt": reserve_proxy,
            }
        )

    return {
        "model": "approximate_post_ladder_capital_reserve",
        "is_exact_bybit_uta_liquidation_model": False,
        "assumptions": {
            "anchor_price_usdt": anchor_price,
            "taker_fee_rate": taker_fee_rate,
            "maintenance_margin_rate": maintenance_margin_rate,
            "shock_reference": "final DCA fill price",
            "reserve_proxy_formula": (
                "deployed margin + floating loss + entry and estimated close fees + "
                "maintenance buffer"
            ),
        },
        "ladder": {
            "dca_levels_exhausted": cycle.dca_level,
            "fills": len(cycle.fills),
            "average_entry": average_entry,
            "final_dca_fill_price": final_fill_price,
            "total_quantity": total_quantity,
            "initial_notional_usdt": initial_notional,
            "entry_notional_usdt": entry_notional,
            "entry_notional_multiple_of_initial": entry_notional / initial_notional,
            "margin_deployed_usdt": margin_deployed,
            "entry_fees_usdt": entry_fees,
        },
        "scenarios": scenarios,
    }


def build_historical_risk_report(
    export: TraderExportData,
    candles: list[Candle],
    calibration_report: dict[str, Any],
    *,
    intrabar_paths: tuple[IntrabarPath, ...] = (
        IntrabarPath.LOW_FIRST,
        IntrabarPath.HIGH_FIRST,
    ),
    taker_fee_rate: float = 0.00055,
    maintenance_margin_rate: float = 0.005,
    tp_change_threshold_percent: float = 0.04,
) -> dict[str, Any]:
    """Compare baseline and calibrated economics on supported regimes."""

    if not candles:
        raise ValueError("at least one candle is required")
    if not intrabar_paths:
        raise ValueError("at least one intrabar path is required")
    calibration = calibration_report.get("calibration", calibration_report)
    calibrated_rows = calibration.get("regimes", [])
    if not calibrated_rows:
        raise ValueError("calibration report has no calibrated regimes")

    regimes = split_regimes(list(export.cycles), threshold=tp_change_threshold_percent)
    ordered_candles = sorted(candles, key=lambda candle: candle.start_ms)
    candle_times = [candle.start_ms for candle in ordered_candles]
    selected: list[tuple[dict[str, Any], list[TraderCycle], list[Candle]]] = []
    for row in calibrated_rows:
        regime_index = int(row["regime_index"])
        if regime_index < 1 or regime_index > len(regimes):
            raise ValueError(f"calibration regime {regime_index} is absent from trader export")
        regime = regimes[regime_index - 1]
        if len(regime) != int(row["completed_cycles"]):
            raise ValueError(f"calibration regime {regime_index} cycle count does not match export")
        if row.get("training_opened_from_ms", regime[0].opened_ms) != regime[0].opened_ms:
            raise ValueError(f"calibration regime {regime_index} start does not match export")
        if row.get("validation_closed_through_ms", regime[-1].closed_ms) != regime[-1].closed_ms:
            raise ValueError(f"calibration regime {regime_index} end does not match export")
        start_ms = regime[0].opened_ms // MINUTE_MS * MINUTE_MS
        end_ms = regime[-1].closed_ms // MINUTE_MS * MINUTE_MS
        left = bisect_left(candle_times, start_ms)
        right = bisect_right(candle_times, end_ms)
        window = ordered_candles[left:right]
        if not window:
            raise ValueError(f"no candles for calibration regime {regime_index}")
        selected.append((row, regime, window))

    comparisons: dict[str, Any] = {}
    stress: dict[str, Any] = {}
    for configuration, parameter_key in (
        ("baseline", "baseline_parameters"),
        ("calibrated", "calibrated_parameters"),
    ):
        comparisons[configuration] = {}
        stress[configuration] = []
        for row, _, _ in selected:
            config = _config_from_summary(row[parameter_key])
            stress[configuration].append(
                {
                    "regime_index": row["regime_index"],
                    "analysis": build_ladder_stress_analysis(
                        config,
                        taker_fee_rate=taker_fee_rate,
                        maintenance_margin_rate=maintenance_margin_rate,
                    ),
                }
            )
        for path in intrabar_paths:
            regime_results: list[dict[str, Any]] = []
            replay_results: list[BacktestResult] = []
            for row, regime, window in selected:
                config = _config_from_summary(row[parameter_key])
                delay = float(row[parameter_key]["reentry_delay_seconds"])
                replay = ReplayEngine(
                    config,
                    intrabar_path=path,
                    taker_fee_rate=taker_fee_rate,
                    reentry_delay_seconds=delay,
                ).run(window)
                replay_results.append(replay)
                regime_results.append(
                    {
                        "regime_index": row["regime_index"],
                        "observed_cycles": len(regime),
                        "parameters": row[parameter_key],
                        "replay": _summarize_replay(replay),
                    }
                )
            comparisons[configuration][path.value] = {
                "aggregate": _aggregate_replays(replay_results),
                "regimes": regime_results,
            }

    covered_cycles = sum(len(regime) for _, regime, _ in selected)
    analyzed_actual_depths = [
        cycle.dca_count
        for _, regime, _ in selected
        for cycle in regime
    ]
    analyzed_indices = {int(row["regime_index"]) for row, _, _ in selected}
    hypothesis = _evaluate_hypothesis(stress)
    return {
        "methodology": {
            "strategy_engine": "botdca.strategy.DcaStrategy via ReplayEngine",
            "scope": "regimes with walk-forward calibration support",
            "economics": "simulated strategy economics, not trader account P&L",
            "fee_assumption": {
                "taker_fee_rate": taker_fee_rate,
                "applied_to": "initial entry, every DCA, and every simulated close",
            },
            "funding": {
                "status": "excluded_no_timestamped_historical_rates",
                "funding_paid_usdt": None,
                "net_pnl_includes_funding": False,
                "extension_point": "botdca.backtest.FundingModel",
            },
            "underwater_timing": (
                "minute-resolution lower bound based on replay marks at candle path points"
            ),
            "aggregate_drawdown": (
                "maximum of regime-level mark-to-market drawdowns because strategy state "
                "resets at detected parameter boundaries"
            ),
            "limitations": [
                "One-minute OHLC does not reveal second-level path ordering.",
                "No slippage, funding, wallet transfers, or cross-margin portfolio offsets are modeled.",
                "The stress reserve is not an exact Bybit liquidation calculation.",
            ],
        },
        "coverage": {
            "export_completed_cycles": len(export.cycles),
            "analyzed_completed_cycles": covered_cycles,
            "excluded_completed_cycles": len(export.cycles) - covered_cycles,
            "analyzed_regime_indices": [row["regime_index"] for row, _, _ in selected],
            "excluded_regime_indices": [
                index for index in range(1, len(regimes) + 1) if index not in analyzed_indices
            ],
        },
        "observed_dca_depth_full_export": _depth_frequency(
            [cycle.dca_count for cycle in export.cycles]
        ),
        "observed_dca_depth_analyzed_regimes": _depth_frequency(analyzed_actual_depths),
        "comparisons": comparisons,
        "tail_stress": stress,
        "hypothesis_evaluation": hypothesis,
    }


def _config_from_summary(summary: dict[str, Any]) -> StrategyConfig:
    return StrategyConfig(
        leverage=int(summary["leverage"]),
        tp_percent=float(summary["tp_percent"]),
        dca_steps=tuple(
            DcaStep(
                drop_percent_from_average=float(step["drop_percent_from_average"]),
                size_multiplier_from_previous=float(step["size_multiplier_from_previous"]),
            )
            for step in summary["dca_steps"]
        ),
    )


def _summarize_replay(result: BacktestResult) -> dict[str, Any]:
    summary = asdict(result)
    summary.pop("cycles")
    depths = [cycle.dca_level for cycle in result.cycles]
    summary["simulated_dca_depth"] = _depth_frequency(depths)
    summary["cycle_duration_seconds"] = _numeric_summary(
        [cycle.duration_seconds for cycle in result.cycles]
    )
    summary["maximum_adverse_excursion_usdt"] = _numeric_summary(
        [cycle.maximum_adverse_excursion_usdt for cycle in result.cycles]
    )
    summary["maximum_adverse_excursion_percent"] = _numeric_summary(
        [cycle.maximum_adverse_excursion_percent for cycle in result.cycles]
    )
    summary["maximum_favorable_excursion_usdt"] = _numeric_summary(
        [cycle.maximum_favorable_excursion_usdt for cycle in result.cycles]
    )
    summary["recovery_duration_seconds"] = _numeric_summary(
        list(result.recovery_durations_seconds)
    )
    return summary


def _aggregate_replays(results: list[BacktestResult]) -> dict[str, Any]:
    depths = [cycle.dca_level for result in results for cycle in result.cycles]
    durations = [cycle.duration_seconds for result in results for cycle in result.cycles]
    adverse_usdt = [
        cycle.maximum_adverse_excursion_usdt
        for result in results
        for cycle in result.cycles
    ]
    adverse_percent = [
        cycle.maximum_adverse_excursion_percent
        for result in results
        for cycle in result.cycles
    ]
    recoveries = [
        duration
        for result in results
        for duration in result.recovery_durations_seconds
    ]
    return {
        "completed_cycles": sum(result.completed_cycles for result in results),
        "gross_realized_pnl_usdt": sum(result.gross_realized_pnl_usdt for result in results),
        "entry_fees_paid_usdt": sum(result.entry_fees_paid_usdt for result in results),
        "exit_fees_paid_usdt": sum(result.exit_fees_paid_usdt for result in results),
        "total_fees_paid_usdt": sum(result.fees_paid_usdt for result in results),
        "realized_cycle_fees_usdt": sum(
            result.realized_cycle_fees_usdt for result in results
        ),
        "open_cycle_entry_fees_paid_usdt": sum(
            result.open_cycle_entry_fees_paid_usdt for result in results
        ),
        "net_realized_pnl_excluding_funding_usdt": sum(
            result.net_realized_pnl_usdt for result in results
        ),
        "final_unrealized_pnl_usdt": sum(
            result.final_unrealized_pnl_usdt for result in results
        ),
        "estimated_open_cycle_exit_fee_usdt": sum(
            result.estimated_open_cycle_exit_fee_usdt for result in results
        ),
        "funding_paid_usdt": None,
        "net_pnl_includes_funding": False,
        "deepest_dca_level": max((result.max_dca_level for result in results), default=0),
        "peak_strategy_margin_usdt": max(
            (result.max_margin_deployed_usdt for result in results), default=0.0
        ),
        "peak_position_notional_usdt": max(
            (result.peak_position_notional_usdt for result in results), default=0.0
        ),
        "worst_floating_pnl_usdt": min(
            (result.worst_floating_pnl_usdt for result in results), default=0.0
        ),
        "maximum_regime_mark_to_market_drawdown_usdt": max(
            (result.max_mark_to_market_drawdown_usdt for result in results), default=0.0
        ),
        "longest_open_cycle_seconds": max(
            (result.longest_open_cycle_seconds for result in results), default=0.0
        ),
        "longest_underwater_seconds": max(
            (result.longest_underwater_seconds for result in results), default=0.0
        ),
        "simulated_dca_depth": _depth_frequency(depths),
        "cycle_duration_seconds": _numeric_summary(durations),
        "maximum_adverse_excursion_usdt": _numeric_summary(adverse_usdt),
        "maximum_adverse_excursion_percent": _numeric_summary(adverse_percent),
        "recovery_duration_seconds": _numeric_summary(recoveries),
    }


def _depth_frequency(depths: list[int]) -> dict[str, Any]:
    if not depths:
        return {"exact": {}, "at_least": {}}
    counts = Counter(depths)
    maximum = max(depths)
    return {
        "exact": {str(level): counts.get(level, 0) for level in range(maximum + 1)},
        "at_least": {
            str(level): sum(depth >= level for depth in depths)
            for level in range(maximum + 1)
        },
    }


def _numeric_summary(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    ordered = sorted(values)
    return {
        "samples": len(ordered),
        "median": median(ordered),
        "p90": _quantile(ordered, 0.90),
        "maximum": ordered[-1],
    }


def _quantile(ordered: list[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _evaluate_hypothesis(stress: dict[str, Any]) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    for configuration, rows in stress.items():
        for row in rows:
            analysis = row["analysis"]
            ladder = analysis["ladder"]
            worst = max(
                analysis["scenarios"],
                key=lambda scenario: scenario["additional_drop_from_final_dca_percent"],
            )
            estimated_roundtrip_fees = (
                ladder["entry_fees_usdt"] + worst["estimated_close_fee_usdt"]
            )
            profiles.append(
                {
                    "configuration": configuration,
                    "regime_index": row["regime_index"],
                    "entry_notional_multiple_of_initial": ladder[
                        "entry_notional_multiple_of_initial"
                    ],
                    "largest_stress_drop_percent": worst[
                        "additional_drop_from_final_dca_percent"
                    ],
                    "largest_stress_floating_loss_usdt": worst["floating_loss_usdt"],
                    "estimated_roundtrip_fees_usdt": estimated_roundtrip_fees,
                    "tail_loss_multiple_of_roundtrip_fees": (
                        worst["floating_loss_usdt"] / estimated_roundtrip_fees
                        if estimated_roundtrip_fees > 0
                        else None
                    ),
                }
            )
    minimum_exposure_multiple = min(
        profile["entry_notional_multiple_of_initial"] for profile in profiles
    )
    minimum_loss_fee_multiple = min(
        profile["tail_loss_multiple_of_roundtrip_fees"]
        for profile in profiles
        if profile["tail_loss_multiple_of_roundtrip_fees"] is not None
    )
    supported = minimum_exposure_multiple >= 10 and minimum_loss_fee_multiple >= 10
    return {
        "hypothesis": (
            "geometric ladder exposure and post-ladder loss dominate trading-fee drag"
        ),
        "status": "supported" if supported else "not_supported",
        "decision_rule": (
            "all tested profiles require at least 10x initial entry notional and the "
            "largest shock loss is at least 10x estimated round-trip fees"
        ),
        "profiles": profiles,
    }
