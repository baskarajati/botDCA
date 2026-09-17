from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Float, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class StrategyEventRecord(Base):
    __tablename__ = "strategy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC), index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ExecutionRecord(Base):
    __tablename__ = "executions"

    execution_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(128), index=True)
    order_link_id: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    price: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float)
    realized_pnl: Mapped[float] = mapped_column(Float)
    execution_time_ms: Mapped[int] = mapped_column(index=True)
    recorded_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class OrderStateRecord(Base):
    __tablename__ = "order_states"

    order_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    order_link_id: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    order_type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[float] = mapped_column(Float)
    qty: Mapped[float] = mapped_column(Float)
    cumulative_executed_qty: Mapped[float] = mapped_column(Float)
    average_price: Mapped[float] = mapped_column(Float)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    reject_reason: Mapped[str] = mapped_column(String(128), default="")
    cancel_type: Mapped[str] = mapped_column(String(128), default="")
    updated_time_ms: Mapped[int] = mapped_column(index=True)
    recorded_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class PositionSnapshotRecord(Base):
    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(16))
    size: Mapped[float] = mapped_column(Float)
    average_entry: Mapped[float] = mapped_column(Float)
    leverage: Mapped[float] = mapped_column(Float)
    mark_price: Mapped[float] = mapped_column(Float)
    liquidation_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    position_idx: Mapped[int] = mapped_column(Integer)
    creation_time_ms: Mapped[int] = mapped_column(index=True)
    recorded_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class Database:
    def __init__(self, url: str) -> None:
        self.engine = create_engine(url, pool_pre_ping=True)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)
