from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from botdca.bybit_events import ExecutionEvent, PositionEvent
from botdca.database import (
    Database,
    ExecutionRecord,
    PositionSnapshotRecord,
    StrategyEventRecord,
)


@dataclass(frozen=True)
class OpenCycleExecutionSummary:
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

    def open_cycle_execution_summary(self, symbol: str) -> OpenCycleExecutionSummary | None:
        statement = (
            select(ExecutionRecord)
            .where(ExecutionRecord.symbol == symbol.upper())
            .order_by(ExecutionRecord.execution_time_ms, ExecutionRecord.execution_id)
        )
        with self.database.session_factory() as session:
            rows = list(session.scalars(statement))

        last_sell_index = -1
        for index, row in enumerate(rows):
            if row.side == "Sell":
                last_sell_index = index
        open_rows = [row for row in rows[last_sell_index + 1 :] if row.side == "Buy"]
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

        total_qty = sum(row.qty for row in open_rows)
        weighted_average = sum(row.price * row.qty for row in open_rows) / total_qty
        last_order_qty = sum(row.qty for row in orders[order_sequence[-1]])
        order_count = len(order_sequence)
        return OpenCycleExecutionSummary(
            order_count=order_count,
            dca_level=max(0, order_count - 1),
            total_buy_qty=total_qty,
            weighted_average_buy_price=weighted_average,
            last_order_qty=last_order_qty,
        )

    def execution_count(self) -> int:
        with self.database.session_factory() as session:
            return len(session.scalars(select(ExecutionRecord.execution_id)).all())
