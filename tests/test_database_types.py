"""Column types must hold real exchange values on PostgreSQL, not only SQLite.

SQLite stores every integer as 64-bit, so a 32-bit INTEGER column for a
millisecond timestamp passes locally and fails on the first real mainnet fill
with ``integer out of range``. These tests pin the types without needing a
PostgreSQL server; the last test runs against one when
``BOTDCA_TEST_POSTGRES_URL`` is set.
"""

import os
import uuid

import pytest
from sqlalchemy import BigInteger, create_engine, insert, inspect, text
from sqlalchemy.dialects.postgresql import psycopg

from botdca.bybit_events import ExecutionEvent, OrderEvent, PositionEvent
from botdca.database import Base, Database, ExecutionRecord
from botdca.persistence import EventStore

# A Bybit execTime observed on mainnet: far above 2**31 - 1.
REAL_EXCHANGE_MS = 1_789_743_736_154


def _millisecond_columns():
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.name.endswith("_ms"):
                yield table.name, column


def test_every_millisecond_column_is_bigint() -> None:
    columns = list(_millisecond_columns())
    assert columns, "expected at least one *_ms column"
    for table, column in columns:
        assert isinstance(column.type, BigInteger), f"{table}.{column.name} is {column.type}"


def test_postgres_insert_binds_milliseconds_as_bigint() -> None:
    statement = insert(ExecutionRecord).values(
        execution_id="exec-compile", execution_time_ms=REAL_EXCHANGE_MS
    )
    compiled = str(statement.compile(dialect=psycopg.dialect()))
    assert "::BIGINT" in compiled
    assert "::INTEGER" not in compiled


def _events(execution_id: str) -> tuple[ExecutionEvent, OrderEvent, PositionEvent]:
    execution = ExecutionEvent(
        symbol="HYPEUSDT",
        order_id=f"order-{execution_id}",
        order_link_id="botdca-open-test",
        execution_id=execution_id,
        side="Buy",
        price=92.22,
        qty=0.06,
        fee=0.00304326,
        realized_pnl=0.0,
        execution_time_ms=REAL_EXCHANGE_MS,
    )
    order = OrderEvent(
        symbol="HYPEUSDT",
        order_id=f"order-{execution_id}",
        order_link_id="botdca-open-test",
        side="Buy",
        order_type="Market",
        status="Filled",
        price=92.22,
        qty=0.06,
        cumulative_executed_qty=0.06,
        average_price=92.22,
        reduce_only=False,
        reject_reason="",
        cancel_type="",
        updated_time_ms=REAL_EXCHANGE_MS,
    )
    position = PositionEvent(
        symbol="HYPEUSDT",
        side="Buy",
        size=0.06,
        average_entry=92.22,
        leverage=24.0,
        mark_price=92.2,
        liquidation_price=None,
        unrealized_pnl=-0.0012,
        position_idx=0,
        creation_time_ms=REAL_EXCHANGE_MS,
    )
    return execution, order, position


def _assert_round_trip(store: EventStore, execution_id: str) -> None:
    execution, order, position = _events(execution_id)
    assert store.record_execution(execution) is True
    store.record_order(order)
    store.record_position(position)
    assert store.latest_position("HYPEUSDT").creation_time_ms == REAL_EXCHANGE_MS


def test_sqlite_round_trips_real_exchange_milliseconds() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    database.create_schema()
    _assert_round_trip(EventStore(database), "exec-sqlite")


@pytest.mark.skipif(
    not os.environ.get("BOTDCA_TEST_POSTGRES_URL"),
    reason="set BOTDCA_TEST_POSTGRES_URL to run against a real PostgreSQL server",
)
def test_postgres_widens_legacy_integer_columns_and_stores_real_milliseconds() -> None:
    url = os.environ["BOTDCA_TEST_POSTGRES_URL"]
    schema = f"botdca_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        database = Database(url)
        database.engine = create_engine(
            url, connect_args={"options": f"-csearch_path={schema}"}
        )
        database.session_factory.configure(bind=database.engine)
        database.create_schema()
        # Recreate the pre-fix schema: 32-bit INTEGER millisecond columns.
        with database.engine.begin() as connection:
            for table, column in (
                ("executions", "execution_time_ms"),
                ("order_states", "updated_time_ms"),
                ("position_snapshots", "creation_time_ms"),
            ):
                connection.execute(
                    text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE INTEGER")
                )

        database.create_schema()

        inspector = inspect(database.engine)
        for table, column in (
            ("executions", "execution_time_ms"),
            ("order_states", "updated_time_ms"),
            ("position_snapshots", "creation_time_ms"),
        ):
            types = {row["name"]: row["type"] for row in inspector.get_columns(table)}
            assert isinstance(types[column], BigInteger), f"{table}.{column}"
        _assert_round_trip(EventStore(database), "exec-postgres")
    finally:
        with admin.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
