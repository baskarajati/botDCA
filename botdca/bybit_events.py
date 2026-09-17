from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _float(value: Any, default: float = 0.0) -> float:
    if value in {None, ""}:
        return default
    return float(value)


@dataclass(frozen=True)
class ExecutionEvent:
    symbol: str
    order_id: str
    order_link_id: str
    execution_id: str
    side: str
    price: float
    qty: float
    fee: float
    realized_pnl: float
    execution_time_ms: int


@dataclass(frozen=True)
class PositionEvent:
    symbol: str
    side: str
    size: float
    average_entry: float
    leverage: float
    mark_price: float
    liquidation_price: float | None
    unrealized_pnl: float
    position_idx: int
    creation_time_ms: int


@dataclass(frozen=True)
class OrderEvent:
    symbol: str
    order_id: str
    order_link_id: str
    side: str
    order_type: str
    status: str
    price: float
    qty: float
    cumulative_executed_qty: float
    average_price: float
    reduce_only: bool
    reject_reason: str
    cancel_type: str
    updated_time_ms: int


def parse_execution_message(
    message: dict[str, Any], *, symbol: str | None = None
) -> list[ExecutionEvent]:
    wanted = symbol.upper() if symbol else None
    events: list[ExecutionEvent] = []
    for item in message.get("data", []):
        if item.get("category") != "linear":
            continue
        item_symbol = str(item.get("symbol", "")).upper()
        if wanted and item_symbol != wanted:
            continue
        events.append(
            ExecutionEvent(
                symbol=item_symbol,
                order_id=str(item.get("orderId", "")),
                order_link_id=str(item.get("orderLinkId", "")),
                execution_id=str(item.get("execId", "")),
                side=str(item.get("side", "")),
                price=_float(item.get("execPrice")),
                qty=_float(item.get("execQty")),
                fee=_float(item.get("execFee")),
                realized_pnl=_float(item.get("execPnl")),
                execution_time_ms=int(item.get("execTime") or message.get("creationTime") or 0),
            )
        )
    return events


def parse_position_message(
    message: dict[str, Any], *, symbol: str | None = None
) -> list[PositionEvent]:
    wanted = symbol.upper() if symbol else None
    creation_time = int(message.get("creationTime") or 0)
    events: list[PositionEvent] = []
    for item in message.get("data", []):
        if item.get("category") != "linear":
            continue
        item_symbol = str(item.get("symbol", "")).upper()
        if wanted and item_symbol != wanted:
            continue
        liquidation = item.get("liqPrice")
        events.append(
            PositionEvent(
                symbol=item_symbol,
                side=str(item.get("side", "")),
                size=_float(item.get("size")),
                average_entry=_float(item.get("entryPrice") or item.get("avgPrice")),
                leverage=_float(item.get("leverage")),
                mark_price=_float(item.get("markPrice")),
                liquidation_price=None if liquidation in {None, ""} else float(liquidation),
                unrealized_pnl=_float(item.get("unrealisedPnl")),
                position_idx=int(item.get("positionIdx") or 0),
                creation_time_ms=creation_time,
            )
        )
    return events


def parse_order_message(
    message: dict[str, Any], *, symbol: str | None = None
) -> list[OrderEvent]:
    wanted = symbol.upper() if symbol else None
    creation_time = int(message.get("creationTime") or 0)
    events: list[OrderEvent] = []
    for item in message.get("data", []):
        if item.get("category") != "linear":
            continue
        item_symbol = str(item.get("symbol", "")).upper()
        if wanted and item_symbol != wanted:
            continue
        events.append(
            OrderEvent(
                symbol=item_symbol,
                order_id=str(item.get("orderId", "")),
                order_link_id=str(item.get("orderLinkId", "")),
                side=str(item.get("side", "")),
                order_type=str(item.get("orderType", "")),
                status=str(item.get("orderStatus", "")),
                price=_float(item.get("price")),
                qty=_float(item.get("qty")),
                cumulative_executed_qty=_float(item.get("cumExecQty")),
                average_price=_float(item.get("avgPrice")),
                reduce_only=bool(item.get("reduceOnly", False)),
                reject_reason=str(item.get("rejectReason", "")),
                cancel_type=str(item.get("cancelType", "")),
                updated_time_ms=int(item.get("updatedTime") or creation_time),
            )
        )
    return events
