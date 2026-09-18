# Multi-page operator console

The console is one secure operator application with three focused pages and
distinct URLs:

- `/` — Overview: current basket, entry controls, capital exposure, readiness
  and worker/private-stream health.
- `/configuration` — Configuration: operator session, exchange credential
  status, strategy, risk limits, runtime/storage and DCA ladder.
- `/journal` — Journal: verified exchange fills, bounded CSV export and recent
  operator actions.

The browser History API changes pages without a document reload. This preserves
the operator token only in JavaScript memory while supporting Back and Forward.
The token is never written to a URL, cookie, local storage or session storage.
Opening a page directly or reloading requires reconnecting from Configuration.

Every page retains mode, freshness, warnings and errors so system state stays
visible. Overview owns all trading actions; Configuration remains read-only
apart from connecting the operator session; Journal owns evidence and export.
The phone header keeps all three destinations visible rather than hiding
navigation. Route changes move focus to the page heading for keyboard and
assistive-technology users.

The FastAPI server returns the same protected console shell for all three paths.
The frontend shows exactly one page view based on the current pathname and marks
the active navigation item with `aria-current="page"`. All operational values
continue to come from the sanitized operations API; no configurable trading
value is duplicated into page markup.

Verification covers direct routes, in-app navigation, Back/Forward, token
continuity across page changes, reload lock behavior, mobile navigation, static
checks, the full Python test suite and packaged static assets. Live trading stays
disabled during verification.
