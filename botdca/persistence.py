from __future__ import annotations

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

    def execution_count(self) -> int:
        with self.database.session_factory() as session:
            return len(session.scalars(select(ExecutionRecord.execution_id)).all())
