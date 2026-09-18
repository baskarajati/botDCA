# Operator UX

## Job and information architecture

Three focused console pages share mode, freshness, authentication state and
server data without persisting the operator token:

- Overview owns per-coin basket controls, shared capital exposure, readiness
  checks and worker health.
- Configuration owns console access, exchange credential status, strategy,
  risk limits, runtime/storage and the DCA ladder.
- Journal owns verified fills, CSV export and operator activity.

Navigation updates the URL and supports Back/Forward without a full reload, so
the memory-only operator token survives normal page changes. A direct load or
reload safely requires reconnecting. No performance page exists until actual
trial observations can support one.

## Controls

- Connect from Configuration with an operator token held only in page memory;
  page close or reload clears it. The mainnet credential form may hold a Bybit
  key and secret only long enough to submit them over a protected connection;
  clear both fields after every result and never place either value in browser
  storage, URLs, logs, responses or later UI state.
- Resume enables entry/re-entry for the selected coin; live resume needs every
  enabled worker healthy and readiness checks passing.
- Pause stops re-entry, not existing DCA management or resting entry orders.
- Close position and pause requires an explicit confirmation. A submitted close
  is not a filled close; display exchange confirmation separately.
- No live-mode toggle or strategy editor: environment configuration is owned by
  the operator, and the strategy remains frozen.

## Configuration

Keep console access, exchange status, three strategy slots, risk limits,
runtime/storage and the DCA ladder together on the Configuration page. Each
slot owns Enable, Coin and Initial margin. Render values and ladder inputs from
the sanitized backend snapshot; do not duplicate operational defaults in page
markup or JavaScript.
Show incremental and cumulative DCA0-DCA8 margin plus the combined enabled-coin
forecast. State that this is planning information, not a reservation, loss
limit, liquidation estimate or close instruction. The shared reserve guard can
block a new order but can never force-close a position.
Explain that server changes require restart. Credential presence is observable,
credential material is not. Credential validation and encrypted storage do not
start the worker or enable entries.

## States and recovery

Keep last-known-good values when polling fails, label them stale with timestamp,
and disable resume/close when status is stale. Pause remains available with a
valid session. Avoid overlapping polls, use request timeouts, stop background
polling and resume visibly. Account or journal failures must not erase unrelated
position data. Empty journal means no recorded fills, never zero historical P&L.

## Validation and accessibility

Inputs have labels, preserved values and inline errors. Controls have visible
focus and 44px targets. Confirmation uses native dialog with focus restoration.
Alerts and action feedback use live regions. Tables have headings, captions and
horizontal overflow within the panel, not the page. Mobile starts with mode,
position and controls, not long configuration prose. Status is never color-only.

## Review checklist

Authentication, successful/failed actions, stale recovery, missing credentials,
database errors, exports, empty/filled journal, keyboard/dialog and narrow layout.
Do not claim accessibility conformance or exchange readiness from unit tests.
