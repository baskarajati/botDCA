# Frozen-strategy trial runbook

Prepared for the operator's **100 USDT equity reference**. Live trading remains disabled.
This is a readiness plan, not exchange approval or evidence of profitability.

## What is frozen

The traded parameters now live in an immutable **strategy version**, not in
loose settings. The default is `greensynergy-reconstructed-v1`, status
**EXPERIMENTAL**: long only, Cross, 24x, take profit +1.09% from the current
weighted-average entry, and a deterministic `next_qty = previous_qty x 1.42`
quantity rule followed by Bybit instrument rounding.

The live ladder is DCA1–DCA8 only, expressed as drops from the **current**
weighted-average entry:

| Level | Trigger | Level | Trigger |
|---|---:|---|---:|
| DCA1 | 1.30% | DCA5 | 3.20% |
| DCA2 | 1.85% | DCA6 | 3.60% |
| DCA3 | 2.40% | DCA7 | 3.80% |
| DCA4 | 2.70% | DCA8 | 4.00% |

DCA9–DCA13 (4.20%, 4.40%, 4.60%, 4.80%, 5.00%) exist in the version as a
**research-only** ladder. They are used for historical comparison, capital
forecasting and stress testing, and are never returned by the live ladder, so
the runtime cannot reach them. Extending autonomous live depth beyond DCA8 is a
separate, explicit decision.

This is a reconstruction from a public trader export. It is not validated, not
optimal and not guaranteed profitable. Autonomous entry and re-entry behaviour
in particular is not proven: GreenSynergy's observed re-entry median is about
six seconds, but the scheduler and admission rules are not visible in the
export, so the production delay stays explicit and configurable rather than
hidden as a magic constant.

The legacy `zuya-reconstructed-v1` version preserves the original PR #1–#3
ladder with its non-uniform multipliers. **Do not confuse the two.** Zuya and
GreenSynergy are different systems reconstructed from different datasets, and
their research outputs are never mixed.

## Per-symbol allocation

One strategy version is shared across up to three operator-selected coins. What
varies per symbol is the starting allocation, not the strategy logic. Every
enabled slot must reference the same strategy version; the console refuses a
mixed-version portfolio.

Two sizing modes are supported, and they are genuinely different configurations:

| Mode | Quantity | Margin |
|---|---|---|
| `FIXED_MARGIN_USDT` | `margin x leverage / price` | the configured margin |
| `FIXED_BASE_QUANTITY` | the configured quantity | `qty x price / leverage` |

"HYPE initial quantity = 0.6 HYPE" is **not** "HYPE initial margin = 1 USDT".
Quantity, notional, margin, leverage and ROI% are tracked separately everywhere.
At a fixed quantity, raising leverage does not change the dollar profit or loss
from a price move; it only reduces reserved initial margin and moves the
liquidation geometry. A higher ROI% is never a stronger strategy edge.

Instrument tick size, quantity step, minimum quantity and minimum notional are
enforced. An allocation that rounds below an instrument limit is refused, never
rounded up.

The third coin is still being researched. It is **not** selected automatically.

## Portfolio risk

The dangerous state is not one coin at DCA8. It is several correlated coins
escalating together against one Bybit Unified Account. The GreenSynergy profiler
finds 35 correlated deep-basket overlaps in the historical dataset, so the three
coins are never evaluated as independent capital pools.

Every entry and DCA is authorised against a coherent portfolio snapshot inside a
single critical section shared by all symbols:

```
read account -> read bot exposure -> size candidate -> project exposure
-> evaluate guards -> submit -> persist
```

An authorised candidate is also held as a pending reservation until the owning
symbol's exposure catches up or the reservation expires, because exchange
balances do not update instantly. Without that, two workers milliseconds apart
would both see the same free equity and both spend it.

Configurable guards:

| Setting | Meaning |
|---|---|
| `BOT_MAX_TOTAL_BOT_MARGIN_USDT` | total committed margin across all symbols |
| `BOT_MAX_TOTAL_BOT_NOTIONAL_USDT` | total bot notional |
| `BOT_MIN_AVAILABLE_BALANCE_USDT` | absolute free-balance floor |
| `BOT_MIN_AVAILABLE_EQUITY_RATIO` | free balance as a fraction of equity |
| `BOT_DEEP_DCA_LEVEL` | the depth at which a basket counts as deep |
| `BOT_MAX_SIMULTANEOUS_DEEP_BASKETS` | how many deep baskets are tolerated at once |
| `BOT_MAX_TOTAL_FLOATING_LOSS_USDT` | optional portfolio floating-loss ceiling |

HYPE at DCA7, ONDO at DCA6 and a third coin at DCA2 is therefore not treated as
three ordinary shallow baskets.

## Max-DCA behaviour

DCA8 does not mean "the bot ran out of code". A basket that reaches its pinned
maximum enters an explicit `MAX_DCA_REACHED` state whose default policy is
`HOLD_AND_ALERT`:

- no further normal DCA is placed, and no DCA9 is invented
- the take profit stays active and is kept installed
- reconciliation continues
- manual reduce-only close remains available
- a critical alert is raised and manual intervention is flagged
- the state survives a restart and is reconstructed from exchange truth

## Position protection

An open bot position must either hold a valid exchange-hosted take profit or be
explicitly unhealthy. A missing take profit is journaled, alerted, surfaced in
readiness and repaired when that is unambiguously safe. Two resting reduce-only
exits are never created: if several are found, the basket is reported as
`protection_ambiguous` and held for a human rather than adding a third.

Coverage is measured against the **position**, not against the quantity the bot
intended to place. A planned quantity that is short of the position would agree
with the resting order, so comparing the two would call the basket protected
while part of it had no exit. That shortfall is what exchange step rounding can
produce: a basket quantity is a float sum of fills, and a sum that drifts a
fraction below a step boundary used to round down a whole step.

## Trial mode

`BOT_TRIAL_MODE=true` constrains the first live run. It can only ever tighten
limits, never loosen them:

| Setting | Effect |
|---|---|
| `BOT_TRIAL_MAX_ACTIVE_SYMBOLS` | refuses to save more enabled slots than this |
| `BOT_TRIAL_MAX_DCA_LEVEL` | reduces the live ladder depth |
| `BOT_TRIAL_MAX_PORTFOLIO_MARGIN_USDT` | tightens the portfolio margin cap |
| `BOT_TRIAL_MANUAL_RESUME_AFTER_RESTART` | refuses any resume that is not the operator's |

The numbers are operator configuration, not recommendations.

`BOT_TRIAL_MANUAL_RESUME_AFTER_RESTART` is enforced, not advisory. A process
starts with the requirement outstanding, and `BotRuntime.resume` raises
`ManualResumeRequiredError` unless the caller identifies itself as the operator.
The console Resume endpoint is the only operator path, and it clears the
requirement for the rest of that process. The strategy also starts paused on its
own, so today the two agree; the guard exists so that a resume added later
cannot quietly restart a funded bot. Outside trial mode the setting does
nothing, like every other `BOT_TRIAL_*` limit. `/api/v1/portfolio` reports
`manual_resume_outstanding`, naming each symbol still waiting for its Resume.
That is live state, so it stays out of the configuration snapshot, whose
fingerprint must identify a configuration and nothing else.

## Activation lifecycle

A configuration carries an explicit status and advances one step at a time:

```
DRAFT -> VALIDATED -> APPROVED_FOR_TESTNET -> APPROVED_FOR_MAINNET_TRIAL
```

Any edit to the traded parameters returns the configuration to `DRAFT`, so
pressing Save can never make a freshly edited experimental strategy
mainnet-live. `RETIRED` is always reachable and never runs. Mainnet still
additionally requires `BOT_MAINNET_PREFLIGHT_APPROVED=true`.

Advance it from the console. The strategy panel shows the current status and
offers exactly the next step, because the API refuses to skip one. The button
is unavailable while a live worker runs, which is the same rule the endpoint
enforces (`PUT /api/v1/configuration/activation` answers 409). Stop the worker,
advance to the required status, then start it again.

An unapproved configuration stops the **worker**, never the API. The service
starts, reports the reason as `live_worker_blocked` on `/health`, and keeps
serving so the configuration can be approved. This matters because activation
can only be advanced through this service: an API that refused to start would
remove the one control that resolves the refusal, and the Compose restart
policy would retry the same failure forever.

## Alerts

Alerts are raised by the runtime and delivered by sinks, so no vendor appears in
strategy code. The journal sink always persists them. An optional generic
webhook is configured with `BOT_ALERT_WEBHOOK_URL` and
`BOT_ALERT_MIN_SEVERITY`; a Telegram, Slack or PagerDuty relay is configuration,
not code, and a failing notification channel never interrupts reconciliation.

Persistent states are deduplicated for `BOT_ALERT_DEDUPE_SECONDS`, so a
condition re-checked every two seconds produces one message, not thousands.
A state that clears re-alerts immediately when it next occurs.

Covered conditions: worker crash, worker stale, private stream disconnected,
reconciliation failure, order rejected, risk-blocked entry, risk-blocked DCA,
deep DCA depth reached, max DCA reached, low available equity, large portfolio
floating loss, missing take profit, protection repaired, and manual intervention
required.

The existing 80 USDT strategy-margin cap and 20% equity reserve are unchanged.
They constrain planned entries, not maximum account loss. The 100 USDT reference
is not an enforced account-equity ceiling, stop loss or liquidation guarantee.
Do not put unrelated capital or manual positions in this experiment's account.
Any funding, account changes and trading activation remain operator actions.

## Local setup and preview

Use Python 3.12 or newer from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

If `.env` does not already exist, copy `.env.example` to it. Never overwrite an
existing configuration. Generate a private operator token locally:

```bash
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Put that token in `BOT_OPERATOR_TOKEN`; do not paste API secrets or this token
into chat, screenshots or Git. Set `BOT_TRIAL_EQUITY_USDT=100`. Keep
`BOT_LIVE_TRADING=false`, `BOT_START_LIVE_WORKER=false` and
`BOT_MAINNET_PREFLIGHT_APPROVED=false`. For a credential-free local preview,
leave both Bybit credentials empty. PostgreSQL is required for live mode; a
private SQLite database is sufficient only for preview/tests.

```bash
docker compose up --build
```

Docker Compose uses PostgreSQL and exposes the console only at
`http://127.0.0.1:8000`. Its bundled database password is development-only;
replace it and the matching `DATABASE_URL` before any funded deployment.
Without Docker, configure an existing database and start one API process:

```bash
.venv/bin/python -m uvicorn botdca.api:app --host 127.0.0.1 --port 8000
```

Enter the operator token on the Configuration page. It remains in page memory
while using the Overview, Configuration and Journal navigation.
Lock/reload clears it but **does not pause a running bot**. Preview controls only
change local state: this is not a paper-trading simulator and generates no fills.
Unmet exchange checks in preview are expected. Account values stay unavailable,
not fabricated from the 100 USDT reference.

## VPS deployment with mounted secrets

The VPS profile runs the API as an unprivileged user with a read-only root
filesystem. It mounts the operator token, database URL and vault master key as
service-scoped Docker secrets; encrypted Bybit credentials live in a separate
persistent volume. Encryption does not protect a fully compromised VPS, so keep
the host patched, the port bound to loopback and remote access behind private
HTTPS/Tailscale. Disable request-body logging in the proxy.

Create the secret files outside this repository. This example generates a
URL-safe database password without printing it:

```bash
sudo install -d -m 0700 /etc/botdca/secrets
sudo .venv/bin/python - <<'PY'
from cryptography.fernet import Fernet
from pathlib import Path
import secrets

root = Path("/etc/botdca/secrets")
password = secrets.token_urlsafe(32)
values = {
    "credential_key": Fernet.generate_key(),
    "database_password": password.encode(),
    "database_url": f"postgresql+psycopg://botdca:{password}@db:5432/botdca".encode(),
    "operator_token": secrets.token_urlsafe(48).encode(),
}
for name, value in values.items():
    path = root / name
    path.write_bytes(value + b"\n")
    path.chmod(0o600)
PY
```

Keep `.env` for non-secret configuration and leave `BYBIT_API_KEY` and
`BYBIT_API_SECRET` blank. Initially keep all activation flags false. Point
Compose at the host secret files. Set `BOTDCA_UID` and `BOTDCA_GID` to the
numeric owner of those `0600` files (`id -u` and `id -g`) so the unprivileged
API process can read its bind-mounted Compose secrets without broadening their
permissions. Then start the VPS profile:

```bash
export BOTDCA_CREDENTIAL_KEY_FILE=/etc/botdca/secrets/credential_key
export BOTDCA_DATABASE_PASSWORD_FILE=/etc/botdca/secrets/database_password
export BOTDCA_DATABASE_URL_FILE=/etc/botdca/secrets/database_url
export BOTDCA_OPERATOR_TOKEN_FILE=/etc/botdca/secrets/operator_token
export BOTDCA_UID=$(id -u)
export BOTDCA_GID=$(id -g)
docker compose -f docker-compose.yml -f docker-compose.vps.yml up -d --build
```

### Scripted redeploy

`scripts/deploy-vps.sh` performs the release-directory deployment above. It takes
any git ref and defaults to `main`:

```bash
scripts/deploy-vps.sh              # deploy main
scripts/deploy-vps.sh v1.2.3       # or a tag, branch or commit
```

It resolves the ref to one commit and records it in `.deployed-commit`, extracts
that commit into a timestamped release directory, carries the existing `.env`
forward, and appends any setting the release introduced using the documented
default from `.env.example` without overwriting existing values. It then builds,
starts, and only repoints `current` once the containers are up, finishing by
printing health, the strategy version, the portfolio guards, the activation gate
and the readiness table.

It deploys in PREVIEW and refuses to run while `BOT_LIVE_TRADING`,
`BOT_START_LIVE_WORKER` or `BOT_MAINNET_PREFLIGHT_APPROVED` is enabled, because
arming is a separate operator step. Set `BOTDCA_ALLOW_ARMED=1` to override that
deliberately. It warns when the portfolio margin cap exceeds the trial equity
reference, since that combination blocks live startup.

It never runs `docker compose down -v`. If the API does not become healthy it
prints the container status and recent logs and tells you the rollback command
rather than leaving `current` pointing at a broken release.

Paths are overridable with `BOTDCA_APP_DIR` (default `~/apps/botdca`),
`BOTDCA_SECRETS_DIR` (default `/etc/botdca/secrets`), `BOTDCA_REPO_URL` and
`BOTDCA_API_ORIGIN`.

On the audited VPS, port `8000` is currently free but the existing tailnet HTTPS
root already belongs to another protected service. Do not replace that route.
After the botDCA containers are healthy, use a separate tailnet-only HTTPS
listener such as `8443` for this console, and include the VPS Tailscale hostname
in `BOT_ALLOWED_HOSTS`. Verify the exact command against the installed Tailscale
version before applying it. A representative layout is:

```text
operator -> https://VPS_TAILSCALE_HOST:8443
         -> Tailscale Serve
         -> http://127.0.0.1:8000
         -> botDCA API container
```

The VPS profile sets `BOT_TRUSTED_HTTPS_PROXY=true`; credential submission still
requires the proxy to send `X-Forwarded-Proto: https`. The operator token remains
required for every protected API request.

Do not run `docker compose down -v` as a restart command: it removes the
database and encrypted-credential volumes. Back up the database and the vault
master key separately. Losing the master key makes the stored Bybit credentials
unrecoverable.

## Direct-mainnet preflight — no orders

The chosen rollout skips testnet. That saves time but removes the safest place
to discover exchange integration mistakes; the first order-path proof will use
real funds. Before any activation, create a dedicated Bybit mainnet key with:

- Unified Trading Account access;
- Contract Trade `Order` and `Position` permissions;
- binding to the VPS public IP;
- no Wallet permissions of any kind, including transfer or withdrawal.

Open the console through the private HTTPS URL, connect with the operator token,
and submit the Bybit key, secret and exact confirmation phrase `MAINNET` on the
Configuration page. The backend performs only account, key-permission and
position reads, then encrypts the credentials. It does **not** start the worker,
enable entries, change account mode or place an order. The fields are cleared
after either success or failure.

Verify HYPEUSDT availability, instrument tick/quantity/minimum-notional rules,
supported leverage, Unified Account type and one-way position mode. The bot
refuses hedge-mode exposure; it does not change account mode automatically.
Start with no manual HYPE position or conflicting orders. Confirm the console
shows the expected account and a flat HYPEUSDT position.

With activation flags still false, restart the API and confirm the encrypted
credentials load, the journal remains intact and the worker stays stopped. No
exchange preflight or funded order has been performed by this preparation.

## Mainnet handoff

Mainnet remains blocked unless `BOT_MAINNET_PREFLIGHT_APPROVED=true`, a strong
operator token, a trial reference, a compatible margin cap and PostgreSQL are
configured. That flag records an acknowledgement; it does not prove the direct
mainnet checks were completed. Obtain explicit owner approval for the exact account,
configuration, funded equity and activation time. Changing the environment and
live flags is outside the browser UI. Do not expose raw HTTP to the Internet;
use an approved private HTTPS/Tailscale path and explicit `BOT_ALLOWED_HOSTS`.

Only after the read-only preflight is accepted should the operator set
`BOT_MAINNET_PREFLIGHT_APPROVED=true`, `BOT_LIVE_TRADING=true` and
`BOT_START_LIVE_WORKER=true`, then recreate the API service. The runtime starts
with re-entry paused. Starting it may still manage an already-owned basket, so
do not activate against an unknown position. Resuming entries is a second,
deliberate UI action and is the point at which a real order may be submitted.

Before activation, independently inspect the actual account equity, available
balance, open positions and orders. Stop if these differ from the approved trial.
The bot does not automatically transfer funds, cap total account deposits or
guarantee that losses remain within a chosen budget.

## Collect evidence without more strategy tuning

The console shows the latest 25 persisted fills and 20 operational events.
CSV export contains the latest 10,000 fills; truncation is reported in the UI
and `X-Export-Truncated` response header. Retain backed-up PostgreSQL data for
the complete journal, including configuration fingerprints and sync/errors.
Execution IDs deduplicate stream and REST recovery rows.

Recovery runs before reconciliation on startup, reconnect and roughly every
30 seconds. It overlaps the persistent checkpoint by one minute, paginates up
to 10,000 rows, and refuses a known gap beyond seven days. A first run searches
only the most recent seven days, not lifetime history. An older open basket or
missing history needs manual recovery; never assume first-run completeness.
Bybit documents the seven-day request window and pagination in its
[execution-history API](https://bybit-exchange.github.io/docs/v5/order/execution).

Fill price, quantity, fee and execution time come from exchange records.
`realized_pnl` is **not account P&L**: REST records missing that field default to
zero, and later duplicate stream rows do not overwrite them.

`GET /api/v1/accounting` attributes strategy results from the bot's own
deterministic order identities, never from a wallet-balance delta, because
deposits, withdrawals, transfers and unrelated positions all move the balance
without being strategy results. It separates realized P&L, entry fees, DCA fees,
close fees, funding and rebates per symbol and for the portfolio.

Stated limitations:

- A fill on a manually placed order is never attributed to the strategy. It is
  preserved in an explicit `unattributed_adjustments_usdt` bucket so the operator
  can see why wallet balance and strategy P&L disagree.
- Funding is charged per symbol at account level, not per basket. When several
  baskets on one symbol span a funding timestamp, funding is attributed to the
  symbol, not split across baskets.
- Rebates appear as negative fees where Bybit reports them that way.
- Bybit's transaction log is paginated and time-bounded. A truncated window is
  reported as `window_truncated`, never assumed to be zero.
- When funding is not queried at all, `funding_usdt` is zero rather than unknown,
  and that is stated in the response's `limitations`.

Keep all private exports outside Git.

Review execution/reconciliation accuracy first, then enough completed cycles
and adverse periods. Do not infer an edge or optimum from a few profitable fills.

## Validation commands

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check botdca tests
.venv/bin/python -m compileall -q botdca
node --check botdca/static/console.js
git diff --check
```

### Optional Bybit testnet/demo lifecycle validation

Credentialed integration tests are excluded from CI by default and must be
requested explicitly. They refuse to run outside testnet/demo:

```bash
BYBIT_API_KEY=... BYBIT_API_SECRET=... BYBIT_TESTNET=true \
BOTDCA_INTEGRATION_SYMBOL=HYPEUSDT \
  .venv/bin/python -m pytest -m bybit_integration
```

The objective is not profitable strategy testing. It is proof that worker start,
leverage configuration, initial order, REST response, execution stream,
persisted fill, reconciliation, TP placement, DCA placement, partial fill,
stream disconnect, execution-history recovery, process restart, basket
reconstruction, manual reduce-only close, confirmed flat state and the paused
state all work against a real exchange.

### GreenSynergy research profiling

The private CSV must stay outside Git. `--workdir` is a caller-supplied working
directory, never a repository path:

```bash
.venv/bin/botdca-profile-greensynergy \
  --csv /private/path/bybit-greensynergy-past-trader-initiated-trades.csv \
  --workdir /private/path/greensynergy-work
```

The export holds three symbols, so it is partitioned per symbol before grouping:
the Zuya parser rejects foreign symbols rather than filtering them, and its
overlap check is global. Basket grouping also uses relative tolerances here,
because the Zuya defaults compare average entry exactly and allow only five
seconds of close skew, which splits many GreenSynergy baskets whose rows share
an average entry to eight decimals. The Zuya defaults are deliberately left
untouched so PR #3's validated numbers do not move.

Use `python -m pytest` to ensure the current checkout, not an older installed
wheel, supplies the tested modules. Local fixture tests are not exchange or
deployment proof. Docker/PostgreSQL runtime, read-only mainnet verification,
funded mainnet orders and VPS supervision remain separate release gates.
