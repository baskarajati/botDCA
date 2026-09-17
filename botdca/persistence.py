from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from botdca.bybit_events import ExecutionEvent, OrderEvent, PositionEvent
from botdca.database import (
    Database,
    ExecutionRecord,
    OrderStateRecord,
    PositionSnapshotRecord,
    StrategyEventRecord,
)


@dataclass(frozen=True)
class OpenCycleExecutionSummary:
    cycle_id: str
    order_count: int
    dca_level: int
    total_buy_qty: float
    weighted_average_buy_price: float
    last_order_qty: float


class EventStore:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record_execution(self, event: ExecutionEvent) -> bool:
        """Persist one fill exactly once. Returns False for a duplicate execId."""
        record = ExecutionRecord(
            execution_id=event.execution_id,
            order_id=event.order_id,
            order_link_id=event.order_link_id,
            symbol=event.symbol,
            side=event.side,
            price=event.price,
            qty=event.qty,
            fee=event.fee,
            realized_pnl=event.realized_pnl,
            execution_time_ms=event.execution_time_ms,
        )
        with self.database.session_factory() as session:
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
        return True

    def record_order(self, event: OrderEvent) -> bool:
        """Upsert the latest state for an order; duplicate/stale events are ignored."""
        with self.database.session_factory() as session:
            record = session.get(OrderStateRecord, event.order_id)
            if record is None:
                record = OrderStateRecord(order_id=event.order_id)
                session.add(record)
            elif event.updated_time_ms < record.updated_time_ms or (
                event.updated_time_ms == record.updated_time_ms
                and event.status == record.status
                and event.cumulative_executed_qty == record.cumulative_executed_qty
                and event.reject_reason == record.reject_reason
                and event.cancel_type == record.cancel_type
            ):
                return False

            record.order_link_id = event.order_link_id
            record.symbol = event.symbol
            record.side = event.side
            record.order_type = event.order_type
            record.status = event.status
            record.price = event.price
            record.qty = event.qty
            record.cumulative_executed_qty = event.cumulative_executed_qty
            record.average_price = event.average_price
            record.reduce_only = event.reduce_only
            record.reject_reason = event.reject_reason
            record.cancel_type = event.cancel_type
            record.updated_time_ms = event.updated_time_ms
            session.commit()
        return True

    def record_position(self, event: PositionEvent) -> int:
        record = PositionSnapshotRecord(
            symbol=event.symbol,
            side=event.side,
            size=event.size,
            average_entry=event.average_entry,
            leverage=event.leverage,
            mark_price=event.mark_price,
            liquidation_price=event.liquidation_price,
            unrealized_pnl=event.unrealized_pnl,
            position_idx=event.position_idx,
            creation_time_ms=event.creation_time_ms,
        )
        with self.database.session_factory() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return record.id

    def record_strategy_event(
        self,
        *,
        event_type: str,
        symbol: str,
        cycle_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        record = StrategyEventRecord(
            event_type=event_type,
            symbol=symbol.upper(),
            cycle_id=cycle_id,
            payload=payload or {},
        )
        with self.database.session_factory() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return record.id

    def latest_position(self, symbol: str) -> PositionSnapshotRecord | None:
        statement = (
            select(PositionSnapshotRecord)
            .where(PositionSnapshotRecord.symbol == symbol.upper())
            .order_by(desc(PositionSnapshotRecord.creation_time_ms), desc(PositionSnapshotRecord.id))
            .limit(1)
        )
        with self.database.session_factory() as session:
            return session.scalar(statement)

    def active_bot_orders(self, symbol: str) -> list[OrderStateRecord]:
        statement = (
            select(OrderStateRecord)
            .where(
                OrderStateRecord.symbol == symbol.upper(),
                OrderStateRecord.order_link_id.like("botdca-%"),
                OrderStateRecord.status.in_(("New", "PartiallyFilled", "Untriggered")),
            )
            .order_by(OrderStateRecord.updated_time_ms, OrderStateRecord.order_id)
        )
        with self.database.session_factory() as session:
            return list(session.scalars(statement))

    def open_cycle_execution_summary(self, symbol: str) -> OpenCycleExecutionSummary | None:
        statement = (
            select(ExecutionRecord)
            .where(ExecutionRecord.symbol == symbol.upper())
            .order_by(ExecutionRecord.execution_time_ms, ExecutionRecord.execution_id)
        )
        with self.database.session_factory() as session:
            rows = list(session.scalars(statement))

        open_rows: list[ExecutionRecord] = []
        net_qty = 0.0
        for row in rows:
            if row.side == "Buy":
                open_rows.append(row)
                net_qty += row.qty
            elif row.side == "Sell":
                net_qty -= row.qty
                if net_qty <= 1e-9:
                    open_rows = []
                    net_qty = 0.0
        if not open_rows:
            return None

        orders: dict[str, list[ExecutionRecord]] = {}
        order_sequence: list[str] = []
        for row in open_rows:
            key = row.order_id or row.execution_id
            if key not in orders:
                orders[key] = []
                order_sequence.append(key)
            orders[key].append(row)

        gross_buy_qty = sum(row.qty for row in open_rows)
        weighted_average = sum(row.price * row.qty for row in open_rows) / gross_buy_qty
        last_order_qty = sum(row.qty for row in orders[order_sequence[-1]])
        order_count = len(order_sequence)
        execution_identity = "|".join(row.execution_id for row in open_rows)
        cycle_id = f"recovered-{sha256(execution_identity.encode()).hexdigest()[:20]}"
        return OpenCycleExecutionSummary(
            cycle_id=cycle_id,
            order_count=order_count,
            dca_level=max(0, order_count - 1),
            total_buy_qty=net_qty,
            weighted_average_buy_price=weighted_average,
            last_order_qty=last_order_qty,
        )

    def entry_generation(self, symbol: str) -> str:
        """Stable identity for the next entry until another sell execution closes a cycle."""
        statement = (
            select(ExecutionRecord)
            .where(
                ExecutionRecord.symbol == symbol.upper(),
                ExecutionRecord.side == "Sell",
            )
            .order_by(desc(ExecutionRecord.execution_time_ms), desc(ExecutionRecord.execution_id))
            .limit(1)
        )
        with self.database.session_factory() as session:
            latest_sell = session.scalar(statement)
        return latest_sell.execution_id if latest_sell is not None else "initial"

    def execution_count(self) -> int:
        with self.database.session_factory() as session:
            return len(session.scalars(select(ExecutionRecord.execution_id)).all())
