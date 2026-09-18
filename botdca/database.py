from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from threading import Lock
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    Integer,
    String,
    create_engine,
    inspect,
    text,
)
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
    execution_time_ms: Mapped[int] = mapped_column(BigInteger, index=True)
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
    updated_time_ms: Mapped[int] = mapped_column(BigInteger, index=True)
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
    creation_time_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    recorded_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC))


class StrategySlotRecord(Base):
    __tablename__ = "strategy_slots"

    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    symbol: Mapped[str] = mapped_column(String(32))
    base_margin_usdt: Mapped[float] = mapped_column(Float)
    sizing_mode: Mapped[str] = mapped_column(String(32), default="fixed_margin_usdt")
    sizing_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    strategy_version_id: Mapped[str] = mapped_column(
        String(64), default="greensynergy-reconstructed-v1"
    )
    activation_status: Mapped[str] = mapped_column(String(32), default="draft")
    updated_at: Mapped[datetime] = mapped_column(
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class AlertRecord(Base):
    """Durable record of every dispatched alert, for audit and deduplication."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(UTC), index=True)
    condition: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), index=True)
    message: Mapped[str] = mapped_column(String(512))
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Set by the runtime when the alerted condition is observed to have cleared.
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)


#: Columns added after the first release. `create_all` never alters an existing
#: table, so an additive migration runs alongside it. Each entry is
#: (table, column, DDL type, default literal).
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str, str], ...] = (
    ("strategy_slots", "sizing_mode", "VARCHAR(32)", "'fixed_margin_usdt'"),
    ("strategy_slots", "sizing_value", "FLOAT", "NULL"),
    (
        "strategy_slots",
        "strategy_version_id",
        "VARCHAR(64)",
        "'greensynergy-reconstructed-v1'",
    ),
    ("strategy_slots", "activation_status", "VARCHAR(32)", "'draft'"),
    ("alerts", "resolved_at", "TIMESTAMP", "NULL"),
)

#: Exchange millisecond timestamps (about 1.8e12) overflow a 32-bit INTEGER.
#: The first releases created these columns as INTEGER, which only SQLite
#: tolerated. Each entry is (table, column) and is widened to BIGINT in place.
_BIGINT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("executions", "execution_time_ms"),
    ("order_states", "updated_time_ms"),
    ("position_snapshots", "creation_time_ms"),
)


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
        self.apply_additive_migrations()
        self.apply_bigint_migrations()

    def apply_additive_migrations(self) -> None:
        """Add columns introduced after a table already existed.

        Only additive, nullable-or-defaulted columns are handled. Nothing is
        dropped, renamed or retyped, so an older deployment upgrades in place
        without losing configuration or journal history.
        """
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        with self.engine.begin() as connection:
            for table, column, column_type, default in _ADDITIVE_COLUMNS:
                if table not in existing_tables:
                    continue
                columns = {row["name"] for row in inspector.get_columns(table)}
                if column in columns:
                    continue
                clause = f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                if default != "NULL":
                    clause += f" DEFAULT {default}"
                connection.execute(text(clause))

    def apply_bigint_migrations(self) -> None:
        """Widen millisecond columns created as 32-bit INTEGER to BIGINT.

        Widening is lossless: every INTEGER value fits in BIGINT. SQLite stores
        every integer as 64-bit already, so only PostgreSQL needs the change.
        """
        if self.engine.dialect.name != "postgresql":
            return
        inspector = inspect(self.engine)
        existing_tables = set(inspector.get_table_names())
        with self.engine.begin() as connection:
            for table, column in _BIGINT_COLUMNS:
                if table not in existing_tables:
                    continue
                types = {row["name"]: row["type"] for row in inspector.get_columns(table)}
                if column in types and not isinstance(types[column], BigInteger):
                    connection.execute(
                        text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE BIGINT")
                    )


class WorkerLease:
    """Hold one live-worker lease per database and symbol."""

    _local_guard: ClassVar[Lock] = Lock()
    _local_leases: ClassVar[set[str]] = set()

    def __init__(self, database: Database, symbol: str) -> None:
        self.database = database
        self.symbol = symbol.upper()
        self._connection = None
        self._local_key: str | None = None

    def acquire(self) -> bool:
        if self._connection is not None or self._local_key is not None:
            return True
        digest = sha256(f"botdca-live-worker:{self.symbol}".encode()).digest()[:8]
        lease_id = int.from_bytes(digest, byteorder="big", signed=True)
        if self.database.engine.dialect.name == "postgresql":
            connection = self.database.engine.connect()
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:lease_id)"),
                    {"lease_id": lease_id},
                ).scalar_one()
            )
            if not acquired:
                connection.close()
                return False
            self._connection = connection
            return True

        key = f"{self.database.engine.url}:{self.symbol}"
        with self._local_guard:
            if key in self._local_leases:
                return False
            self._local_leases.add(key)
        self._local_key = key
        return True

    def release(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            digest = sha256(f"botdca-live-worker:{self.symbol}".encode()).digest()[:8]
            lease_id = int.from_bytes(digest, byteorder="big", signed=True)
            connection.execute(
                text("SELECT pg_advisory_unlock(:lease_id)"),
                {"lease_id": lease_id},
            )
            connection.close()

        key = self._local_key
        self._local_key = None
        if key is not None:
            with self._local_guard:
                self._local_leases.discard(key)
