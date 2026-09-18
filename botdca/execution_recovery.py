"""Bounded, paginated recovery of stream-missed trade fills before reconciliation."""

from time import time

from botdca.bybit_events import parse_execution_message
from botdca.bybit_exchange import BybitApiError

WEEK_MS = 7 * 24 * 60 * 60 * 1000


class ExecutionRecovery:
    def __init__(self, exchange, store, symbol, *, clock=time):
        self.exchange, self.store, self.symbol, self.clock = exchange, store, symbol, clock

    def __call__(self):
        end = int(self.clock() * 1000)
        checkpoint = self.store.execution_recovery_checkpoint(self.symbol)
        if checkpoint is not None and end - checkpoint > WEEK_MS - 60000:
            raise BybitApiError(
                "Execution recovery gap exceeds seven days. Reconcile history manually before resuming."
            )
        start = max(
            end - WEEK_MS, (checkpoint - 60000) if checkpoint is not None else end - WEEK_MS
        )
        cursor, seen, inserted = "", set(), 0
        for _ in range(100):
            response = self.exchange._require_ok(
                self.exchange.session.get_executions(
                    category="linear",
                    symbol=self.symbol,
                    startTime=start,
                    endTime=end,
                    limit=100,
                    **({"cursor": cursor} if cursor else {}),
                )
            )
            result = response.get("result", {})
            rows = [{**row, "category": "linear"} for row in result.get("list", [])]
            events = parse_execution_message({"data": rows}, symbol=self.symbol)
            for event in events:
                if (
                    not event.execution_id
                    or not event.order_id
                    or event.price <= 0
                    or event.qty <= 0
                ):
                    raise BybitApiError("Invalid execution history row; recovery incomplete.")
                inserted += int(self.store.record_execution(event))
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                self.store.record_strategy_event(
                    event_type="EXECUTION_RECOVERY",
                    symbol=self.symbol,
                    payload={
                        "start_ms": start,
                        "end_ms": end,
                        "inserted_fills": inserted,
                        "realized_pnl_missing_from_rest_defaults_to_zero": True,
                    },
                )
                return
            if cursor in seen:
                raise BybitApiError("Execution history pagination repeated; recovery incomplete.")
            seen.add(cursor)
        raise BybitApiError(
            "Execution history exceeds the 10,000-row recovery bound; reconcile manually."
        )
