# Three-symbol portfolio configuration

## Scope

Allow one Bybit Unified Trading Account to run the frozen long-only DCA
strategy on one to three distinct USDT perpetual symbols. Each enabled slot
owns a symbol and an initial margin (the DCA0 margin). There is no per-symbol
maximum-margin field and no forecast-driven close behavior.

## Safety model

The wallet remains shared. Each symbol has an independent strategy state,
orders, journal identity and worker lease, while a shared portfolio lock
serializes account reads and order decisions. Existing account-reserve checks
continue to block new entries or DCA orders when the shared available balance
would cross the reserve floor. They never close an open position. TP management
and explicit operator closes remain symbol-specific.

Configuration changes are accepted only while all live workers are stopped and
no local basket is open. Symbols must be unique enabled Bybit linear USDT
perpetuals. Saving replaces all three slots atomically in PostgreSQL, rebuilds
the preview runtimes, records an audit event and does not start workers or
enable entries.

## Forecast

For every slot, calculate DCA0 through the configured DCA8 ladder from a
normalized entry price. Each row shows the incremental margin and cumulative
margin implied by that slot's initial margin, leverage, trigger drops and size
multipliers. The combined full-ladder forecast sums enabled slots. It is a
planning estimate, not a reservation, loss limit, liquidation estimate or
close instruction. The UI warns when the combined forecast exceeds the trial
equity reference but does not reject the configuration.

## Interface

Configuration presents three editable slot cards with Enable, Symbol and
Initial margin fields, one Save strategy slots action, inline validation and a
responsive forecast table. The current global margin cap is described as an
entry guard rather than a close trigger and is not editable per coin.

Overview renders each enabled symbol as its own basket with Enable entries,
Pause new entries and Close position controls. Destructive confirmation names
the selected symbol. Journal and account summaries aggregate configured symbols
without merging their strategy state.

## Verification

Cover slot validation and persistence, forecast arithmetic, atomic API updates,
worker-off mutation gates, multi-runtime snapshots, symbol-specific controls,
shared-lock worker construction, HTML/JavaScript syntax and the existing full
test suite. Deployment remains preview-only until a separate activation step.
