from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from math import isfinite

from sqlalchemy import select

from botdca.database import Database, StrategySlotRecord
from botdca.domain import DEFAULT_DCA_STEPS, DcaStep

MAX_STRATEGY_SLOTS = 3
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,24}USDT$")


@dataclass(frozen=True, slots=True)
class StrategySlot:
    slot: int
    enabled: bool
    symbol: str
    base_margin_usdt: float

    def normalized(self) -> StrategySlot:
        return StrategySlot(
            slot=self.slot,
            enabled=self.enabled,
            symbol=self.symbol.strip().upper(),
            base_margin_usdt=self.base_margin_usdt,
        )


def default_strategy_slots(symbol: str, base_margin_usdt: float) -> tuple[StrategySlot, ...]:
    return (
        StrategySlot(1, True, symbol.upper(), base_margin_usdt),
        StrategySlot(2, False, "BTCUSDT", base_margin_usdt),
        StrategySlot(3, False, "ETHUSDT", base_margin_usdt),
    )


def validate_strategy_slots(slots: list[StrategySlot] | tuple[StrategySlot, ...]) -> tuple[StrategySlot, ...]:
    normalized = tuple(slot.normalized() for slot in slots)
    if len(normalized) != MAX_STRATEGY_SLOTS:
        raise ValueError("Configure exactly three strategy slots.")
    if tuple(slot.slot for slot in normalized) != (1, 2, 3):
        raise ValueError("Strategy slots must be numbered 1, 2 and 3.")

    enabled_symbols: set[str] = set()
    enabled_count = 0
    for slot in normalized:
        if not isfinite(slot.base_margin_usdt) or slot.base_margin_usdt <= 0:
            raise ValueError(f"Slot {slot.slot} initial margin must be greater than zero.")
        if slot.base_margin_usdt > 1_000_000:
            raise ValueError(f"Slot {slot.slot} initial margin is unreasonably large.")
        if not SYMBOL_PATTERN.fullmatch(slot.symbol):
            raise ValueError(
                f"Slot {slot.slot} symbol must be a USDT perpetual such as HYPEUSDT."
            )
        if not slot.enabled:
            continue
        enabled_count += 1
        if slot.symbol in enabled_symbols:
            raise ValueError(f"Enabled symbol {slot.symbol} is configured more than once.")
        enabled_symbols.add(slot.symbol)
    if enabled_count == 0:
        raise ValueError("Enable at least one strategy slot.")
    return normalized


def enabled_strategy_slots(slots: tuple[StrategySlot, ...]) -> tuple[StrategySlot, ...]:
    return tuple(slot for slot in slots if slot.enabled)


def margin_forecast(
    base_margin_usdt: float,
    *,
    leverage: int,
    dca_steps: tuple[DcaStep, ...] = DEFAULT_DCA_STEPS,
    max_dca_level: int | None = None,
) -> list[dict[str, float | int]]:
    """Forecast ladder margin from normalized price; never an execution or close rule."""
    if not isfinite(base_margin_usdt) or base_margin_usdt <= 0:
        raise ValueError("base margin must be positive and finite")
    if leverage <= 0:
        raise ValueError("leverage must be positive")

    steps = dca_steps if max_dca_level is None else dca_steps[:max_dca_level]
    entry_price = 100.0
    last_price = entry_price
    last_qty = base_margin_usdt * leverage / entry_price
    total_qty = last_qty
    total_notional = entry_price * last_qty
    cumulative_margin = base_margin_usdt
    rows: list[dict[str, float | int]] = [
        {
            "level": 0,
            "drop_percent": 0.0,
            "size_multiplier": 1.0,
            "incremental_margin_usdt": base_margin_usdt,
            "cumulative_margin_usdt": cumulative_margin,
        }
    ]
    for index, step in enumerate(steps, start=1):
        average_entry = total_notional / total_qty
        fill_price = average_entry * (1 - step.drop_percent_from_average / 100)
        qty = last_qty * step.size_multiplier_from_previous
        incremental_margin = fill_price * qty / leverage
        cumulative_margin += incremental_margin
        total_qty += qty
        total_notional += fill_price * qty
        rows.append(
            {
                "level": index,
                "drop_percent": step.drop_percent_from_average,
                "size_multiplier": step.size_multiplier_from_previous,
                "incremental_margin_usdt": incremental_margin,
                "cumulative_margin_usdt": cumulative_margin,
            }
        )
        last_price = fill_price
        last_qty = qty
    _ = last_price
    return rows


def serialized_slot(
    slot: StrategySlot,
    *,
    leverage: int,
    dca_steps: tuple[DcaStep, ...] = DEFAULT_DCA_STEPS,
    max_dca_level: int | None = None,
) -> dict:
    result = asdict(slot)
    forecast = margin_forecast(
        slot.base_margin_usdt,
        leverage=leverage,
        dca_steps=dca_steps,
        max_dca_level=max_dca_level,
    )
    result["margin_forecast"] = forecast
    result["full_ladder_margin_usdt"] = forecast[-1]["cumulative_margin_usdt"]
    return result


class StrategySlotStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def load(self, *, default_symbol: str, default_base_margin_usdt: float) -> tuple[StrategySlot, ...]:
        with self.database.session_factory() as session:
            rows = list(session.scalars(select(StrategySlotRecord).order_by(StrategySlotRecord.slot)))
        if not rows:
            return default_strategy_slots(default_symbol, default_base_margin_usdt)
        return validate_strategy_slots(
            [
                StrategySlot(row.slot, row.enabled, row.symbol, row.base_margin_usdt)
                for row in rows
            ]
        )

    def replace(self, slots: list[StrategySlot] | tuple[StrategySlot, ...]) -> tuple[StrategySlot, ...]:
        validated = validate_strategy_slots(slots)
        with self.database.session_factory() as session:
            existing = {
                row.slot: row
                for row in session.scalars(
                    select(StrategySlotRecord).order_by(StrategySlotRecord.slot)
                )
            }
            for slot in validated:
                row = existing.get(slot.slot)
                if row is None:
                    row = StrategySlotRecord(slot=slot.slot)
                    session.add(row)
                row.enabled = slot.enabled
                row.symbol = slot.symbol
                row.base_margin_usdt = slot.base_margin_usdt
            session.commit()
        return validated
