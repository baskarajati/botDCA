# Live worker production-hardening design

## Deployment boundary

The authenticated worker runs in the FastAPI process lifespan so the existing pause,
resume, manual-close, dashboard, and worker all share one `BotRuntime`. Startup requires
both `BOT_LIVE_TRADING=true` and `BOT_START_LIVE_WORKER=true`; either false leaves the
worker stopped. Credentials are validated before any stream or exchange mutation is
attempted. The application constructs the database, event store, authenticated REST
adapter, instrument rules, private stream, reconciliation service, and worker in one
explicit factory. Shutdown stops the worker and private stream.

The deployment remains one API process. A database-backed worker lease rejects a second
live worker for the same symbol, protecting against accidental multi-process or
multi-replica deployment. Dry-run API/dashboard startup remains the default and requires
neither credentials nor a reachable database.

## Order idempotency and recovery

Every intended entry, take-profit, and DCA order receives a deterministic Bybit
`orderLinkId` computed from its role, symbol, side, quantity, price, and reduce-only
state. The REST adapter checks current and historical orders before submitting. If the
submission response is lost or raises after Bybit accepted the request, it queries the
same client identity and returns the recovered acknowledgement. Retrying the same plan
therefore cannot create a second order.

For an open position, reconciliation reads exchange truth and persisted fills first.
The service reuses exact matching TP/DCA orders. It installs missing TP protection before
removing stale TP orders. For entry-increasing DCA orders it removes stale orders before
installing the new order, preventing two active DCA liabilities. It cancels only botDCA
orders, never unrelated account orders.

## Disconnects, restart, and tests

The stream exposes connection health. Each worker pass verifies it and restarts a
disconnected private stream before reconciliation, recording the recovery. A worker
exception pauses re-entry while continuing later reconciliation attempts, so an open
position can regain protection after connectivity returns.

Tests cover opt-in construction, default-off behavior, duplicate worker lease rejection,
reuse of orders across reconstructed services, uncertain-submit recovery, stale-order
replacement ordering, stream reconnection, and clean lifespan shutdown. Unit tests use
in-memory substitutes; CI runs lint and the full test suite without real credentials or
live exchange mutations.
