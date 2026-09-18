# Frozen-strategy trial console

User changed the objective from research optimization to implementation and
trial preparation. Preserve current strategy defaults and all local research
changes. No live activation, orders, deployment, commit, push or PR update.

Recommended approach: finish the existing FastAPI/HTML console with protected
controls, durable journal reads/CSV export, honest readiness and a runbook.
A separate JS framework would duplicate the API and increase release work;
a research dashboard would not meet the operator's job. No simulator is added
or implied: preview controls manipulate local state only.

One DOM-rendered console application with three URL-addressable pages, bounded
tables (25 fills, 20 events), five-second
polling with no overlap, timeouts and background suspension. No charts, GPU,
external fonts or third-party browser dependencies. The selected visual
direction follows the dense, square-edged Coin Scanner workbench: near-black
green surfaces, thin grid borders, off-white type and a chartreuse focal accent.
Position is the primary reading path on desktop and phone. Overview owns live
operations, Configuration owns all server-backed settings, and Journal owns
fills and operator activity. Client-side page navigation preserves the token in
memory without storing it; direct loads and reloads reconnect. No URL-stored
secrets or persistent browser tokens.

The Configuration section contains console access, exchange credential status,
strategy, risk limits, runtime/storage and the DCA ladder. The operator token is
entered there and kept only in page memory. Bybit key and secret values remain
server-managed and are never returned to the browser; the page exposes only
configured/not-configured status. Visible strategy and risk values come from the
sanitized operations snapshot rather than duplicated HTML or JavaScript values.

Operator token protects API data/mutations when configured; mutations are
disabled without it. Trusted-host checks and loopback Docker publication bound
the default surface. Live startup requires an operator token, explicit trial
equity reference, margin budget consistency and PostgreSQL worker lease.
Mainnet additionally requires an operator-recorded preflight approval flag.
This flag is an acknowledgement, not proof that exchange testing occurred.

Journal reads reuse the worker store or connect lazily to the configured DB.
An unavailable journal is a visible blocker, not fabricated empty history.
Execution export is explicitly bounded to the latest 10,000 fills and identifies
truncation. Config snapshots record the frozen strategy, risk, re-entry delay,
mode and instrument increments without credentials. Manual controls are audited.
Pause/exit are not blocked solely by logging failure; resume is.

Readiness separates preview, configured-live, running worker, account connectivity,
journal availability and manual exchange test gates. Margin cap is not a loss
cap; the UI must not imply the reference equity bounds possible losses.

QA: existing suite plus API/auth/readiness/export/config snapshot tests; browser
desktop/mobile, action confirmation, lock, stale state and error recovery.
Local preview uses explicitly disabled live flags and blank exchange credentials.
Docker and real Bybit testnet/mainnet checks remain separately reported if not run.
