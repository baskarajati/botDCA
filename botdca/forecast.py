"""Portfolio capital forecasting and approximate stress.

Everything here is a forecast from a normalized price path, never an execution
rule and never a close rule. The stress figures are an APPROXIMATE model and
are explicitly NOT an exact Bybit Unified Account liquidation calculation:
actual survival also depends on real fill prices, maintenance-margin tiers,
fees, funding, correlated drawdown and account state.
"""

from __future__ import annotations

from dataclasses import dataclass

from botdca.sizing import InitialAllocation, SizingMode
from botdca.strategy_version import StrategyVersion, research_dca_steps_for

STRESS_DROPS: tuple[float, ...] = (5.0, 10.0, 20.0)

APPROXIMATION_NOTE = (
    "APPROXIMATE STRESS MODEL. NOT AN EXACT BYBIT UTA LIQUIDATION CALCULATION."
)


@dataclass(frozen=True, slots=True)
class LadderRow:
    level: int
    research_only: bool
    trigger_drop_percent: float
    fill_price: float
    incremental_qty: float
    cumulative_qty: float
    incremental_margin_usdt: float
    cumulative_margin_usdt: float
    position_notional_usdt: float
    weighted_average_entry: float

    def describe(self) -> dict:
        return {
            "level": self.level,
            "research_only": self.research_only,
            "trigger_drop_percent": self.trigger_drop_percent,
            "fill_price": self.fill_price,
            "incremental_qty": self.incremental_qty,
            "cumulative_qty": self.cumulative_qty,
            "incremental_margin_usdt": self.incremental_margin_usdt,
            "cumulative_margin_usdt": self.cumulative_margin_usdt,
            "position_notional_usdt": self.position_notional_usdt,
            "weighted_average_entry": self.weighted_average_entry,
        }


@dataclass(frozen=True, slots=True)
class SymbolForecast:
    symbol: str
    strategy_version_id: str
    reference_price: float
    leverage: int
    allocation_mode: str
    allocation_value: float
    initial_qty: float
    initial_margin_usdt: float
    max_live_dca_level: int
    rows: tuple[LadderRow, ...]

    def row_at(self, level: int) -> LadderRow:
        if level < 0 or level >= len(self.rows):
            raise IndexError(f"DCA level {level} is outside the forecast ladder")
        return self.rows[level]

    def describe(self) -> dict:
        return {
            "symbol": self.symbol,
            "strategy_version_id": self.strategy_version_id,
            "reference_price": self.reference_price,
            "leverage": self.leverage,
            "allocation": {"mode": self.allocation_mode, "value": self.allocation_value},
            "initial_qty": self.initial_qty,
            "initial_margin_usdt": self.initial_margin_usdt,
            "max_live_dca_level": self.max_live_dca_level,
            "max_research_dca_level": len(self.rows) - 1,
            "rows": [row.describe() for row in self.rows],
        }


def forecast_symbol(
    *,
    symbol: str,
    version: StrategyVersion,
    allocation: InitialAllocation,
    reference_price: float = 100.0,
    include_research_levels: bool = True,
) -> SymbolForecast:
    """Walk the ladder from a reference price, reporting all four quantities.

    Levels beyond `version.max_live_dca_level` are flagged `research_only` and
    are never reachable by the live runtime.
    """
    if reference_price <= 0:
        raise ValueError("reference_price must be positive")

    leverage = version.leverage
    if allocation.mode is SizingMode.FIXED_MARGIN_USDT:
        initial_qty = allocation.value * leverage / reference_price
    else:
        initial_qty = allocation.value
    initial_margin = initial_qty * reference_price / leverage

    steps = research_dca_steps_for(version)
    if not include_research_levels:
        steps = steps[: version.max_live_dca_level]

    cumulative_qty = initial_qty
    cumulative_notional = initial_qty * reference_price
    cumulative_margin = initial_margin
    last_qty = initial_qty

    rows = [
        LadderRow(
            level=0,
            research_only=False,
            trigger_drop_percent=0.0,
            fill_price=reference_price,
            incremental_qty=initial_qty,
            cumulative_qty=cumulative_qty,
            incremental_margin_usdt=initial_margin,
            cumulative_margin_usdt=cumulative_margin,
            position_notional_usdt=cumulative_notional,
            weighted_average_entry=reference_price,
        )
    ]

    for index, step in enumerate(steps, start=1):
        weighted_average = cumulative_notional / cumulative_qty
        fill_price = weighted_average * (1 - step.drop_percent_from_average / 100)
        qty = last_qty * step.size_multiplier_from_previous
        incremental_margin = fill_price * qty / leverage
        cumulative_qty += qty
        cumulative_notional += fill_price * qty
        cumulative_margin += incremental_margin
        last_qty = qty
        rows.append(
            LadderRow(
                level=index,
                research_only=version.is_research_only_level(index),
                trigger_drop_percent=step.drop_percent_from_average,
                fill_price=fill_price,
                incremental_qty=qty,
                cumulative_qty=cumulative_qty,
                incremental_margin_usdt=incremental_margin,
                cumulative_margin_usdt=cumulative_margin,
                position_notional_usdt=cumulative_notional,
                weighted_average_entry=cumulative_notional / cumulative_qty,
            )
        )

    return SymbolForecast(
        symbol=symbol.upper(),
        strategy_version_id=version.version_id,
        reference_price=reference_price,
        leverage=leverage,
        allocation_mode=str(allocation.mode),
        allocation_value=allocation.value,
        initial_qty=initial_qty,
        initial_margin_usdt=initial_margin,
        max_live_dca_level=version.max_live_dca_level,
        rows=tuple(rows),
    )


@dataclass(frozen=True, slots=True)
class StressPoint:
    drop_percent: float
    price_factor: float
    floating_pnl_usdt: float
    equity_consumption_usdt: float
    approximate_maintenance_margin_usdt: float
    approximate_equity_buffer_usdt: float | None

    def describe(self) -> dict:
        return {
            "drop_percent": self.drop_percent,
            "floating_pnl_usdt": self.floating_pnl_usdt,
            "equity_consumption_usdt": self.equity_consumption_usdt,
            "approximate_maintenance_margin_usdt": self.approximate_maintenance_margin_usdt,
            "approximate_equity_buffer_usdt": self.approximate_equity_buffer_usdt,
        }


@dataclass(frozen=True, slots=True)
class PortfolioScenario:
    name: str
    levels: dict[str, int]
    contains_research_levels: bool
    total_qty_by_symbol: dict[str, float]
    total_margin_usdt: float
    total_notional_usdt: float
    stress: tuple[StressPoint, ...]

    def describe(self) -> dict:
        return {
            "name": self.name,
            "levels": dict(self.levels),
            "contains_research_levels": self.contains_research_levels,
            "total_qty_by_symbol": dict(self.total_qty_by_symbol),
            "total_margin_usdt": self.total_margin_usdt,
            "total_notional_usdt": self.total_notional_usdt,
            "stress": [point.describe() for point in self.stress],
            "note": APPROXIMATION_NOTE,
        }


def _stress(
    forecasts: dict[str, SymbolForecast],
    levels: dict[str, int],
    *,
    account_equity_usdt: float | None,
    maintenance_margin_rate: float,
) -> tuple[StressPoint, ...]:
    points: list[StressPoint] = []
    for drop in STRESS_DROPS:
        factor = 1 - drop / 100
        floating = 0.0
        notional_after = 0.0
        for symbol, level in levels.items():
            row = forecasts[symbol].row_at(level)
            stressed_price = row.weighted_average_entry * factor
            floating += (stressed_price - row.weighted_average_entry) * row.cumulative_qty
            notional_after += stressed_price * row.cumulative_qty
        maintenance = notional_after * maintenance_margin_rate
        buffer_usdt = (
            None
            if account_equity_usdt is None
            else account_equity_usdt + floating - maintenance
        )
        points.append(
            StressPoint(
                drop_percent=drop,
                price_factor=factor,
                floating_pnl_usdt=floating,
                equity_consumption_usdt=-floating,
                approximate_maintenance_margin_usdt=maintenance,
                approximate_equity_buffer_usdt=buffer_usdt,
            )
        )
    return tuple(points)


def build_scenario(
    name: str,
    forecasts: dict[str, SymbolForecast],
    levels: dict[str, int],
    *,
    max_live_dca_level: int,
    account_equity_usdt: float | None = None,
    maintenance_margin_rate: float = 0.005,
) -> PortfolioScenario:
    total_margin = 0.0
    total_notional = 0.0
    qty_by_symbol: dict[str, float] = {}
    for symbol, level in levels.items():
        row = forecasts[symbol].row_at(level)
        total_margin += row.cumulative_margin_usdt
        total_notional += row.position_notional_usdt
        qty_by_symbol[symbol] = row.cumulative_qty
    return PortfolioScenario(
        name=name,
        levels=dict(levels),
        contains_research_levels=any(level > max_live_dca_level for level in levels.values()),
        total_qty_by_symbol=qty_by_symbol,
        total_margin_usdt=total_margin,
        total_notional_usdt=total_notional,
        stress=_stress(
            forecasts,
            levels,
            account_equity_usdt=account_equity_usdt,
            maintenance_margin_rate=maintenance_margin_rate,
        ),
    )


def portfolio_scenarios(
    forecasts: dict[str, SymbolForecast],
    *,
    max_live_dca_level: int,
    uniform_levels: tuple[int, ...] = (4, 6, 8),
    account_equity_usdt: float | None = None,
    maintenance_margin_rate: float = 0.005,
) -> list[PortfolioScenario]:
    """Combined scenarios: every coin at the same depth, plus one-deep mixes."""
    if not forecasts:
        return []
    symbols = sorted(forecasts)
    scenarios: list[PortfolioScenario] = []

    for level in uniform_levels:
        capped = {s: min(level, len(forecasts[s].rows) - 1) for s in symbols}
        scenarios.append(
            build_scenario(
                f"all coins DCA{level}",
                forecasts,
                capped,
                max_live_dca_level=max_live_dca_level,
                account_equity_usdt=account_equity_usdt,
                maintenance_margin_rate=maintenance_margin_rate,
            )
        )

    deep, shallow = max(uniform_levels), min(uniform_levels)
    for symbol in symbols:
        levels = {
            other: min(deep if other == symbol else shallow, len(forecasts[other].rows) - 1)
            for other in symbols
        }
        others = len(symbols) - 1
        scenarios.append(
            build_scenario(
                f"{symbol} DCA{deep} + {others} x DCA{shallow}",
                forecasts,
                levels,
                max_live_dca_level=max_live_dca_level,
                account_equity_usdt=account_equity_usdt,
                maintenance_margin_rate=maintenance_margin_rate,
            )
        )
    return scenarios


def worst_case_scenario(
    forecasts: dict[str, SymbolForecast],
    *,
    max_live_dca_level: int,
    account_equity_usdt: float | None = None,
) -> PortfolioScenario | None:
    """Every enabled coin simultaneously at the deepest live level."""
    if not forecasts:
        return None
    levels = {
        symbol: min(max_live_dca_level, len(forecast.rows) - 1)
        for symbol, forecast in forecasts.items()
    }
    return build_scenario(
        f"all coins at live maximum DCA{max_live_dca_level}",
        forecasts,
        levels,
        max_live_dca_level=max_live_dca_level,
        account_equity_usdt=account_equity_usdt,
    )


def research_expansion(
    forecasts: dict[str, SymbolForecast], *, max_live_dca_level: int
) -> dict:
    """How much further capital the research-only ladder would demand.

    Research and capital planning only. These levels are never traded live.
    """
    live_total = 0.0
    research_total = 0.0
    for forecast in forecasts.values():
        live_level = min(max_live_dca_level, len(forecast.rows) - 1)
        live_total += forecast.row_at(live_level).cumulative_margin_usdt
        research_total += forecast.rows[-1].cumulative_margin_usdt
    return {
        "live_max_level": max_live_dca_level,
        "live_total_margin_usdt": live_total,
        "research_max_level": max((len(f.rows) - 1 for f in forecasts.values()), default=0),
        "research_total_margin_usdt": research_total,
        "expansion_multiple": (research_total / live_total) if live_total > 0 else None,
        "note": "Research-only. DCA levels beyond the live maximum are never traded.",
    }
