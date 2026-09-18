# Frozen-strategy trial runbook

Prepared for the operator's **100 USDT equity reference**. Live trading remains disabled.
This is a readiness plan, not exchange approval or evidence of profitability.

## What is frozen

HYPEUSDT, long only, 24x leverage, 1 USDT base margin, 1.09% TP,
30-second re-entry delay and the existing eight-level DCA ladder. No DCA9–10
rules have been invented. The entry rule is the existing runtime rule, not a
proven reconstruction of the original trader's signal.

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
zero, and later duplicate stream rows do not overwrite them. Funding, transfers
and other account cash flows are not collected here. Obtain account transaction
history separately before evaluating returns. Keep all private exports outside Git.

Review execution/reconciliation accuracy first, then enough completed cycles
and adverse periods. Do not infer an edge or optimum from a few profitable fills.

## Validation commands

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check botdca tests
node --check botdca/static/console.js
```

Use `python -m pytest` to ensure the current checkout, not an older installed
wheel, supplies the tested modules. Local fixture tests are not exchange or
deployment proof. Docker/PostgreSQL runtime, read-only mainnet verification,
funded mainnet orders and VPS supervision remain separate release gates.
