#!/usr/bin/env bash
#
# Deploy botDCA to a VPS using the hardened Compose profile.
#
#   scripts/deploy-vps.sh [REF]
#
# REF is any git ref or commit (default: main). The exact commit is resolved
# once and recorded, so a deploy is reproducible.
#
# This script deploys in PREVIEW. It refuses to run while any activation flag
# is enabled, because arming live trading is a separate, deliberate operator
# step documented in docs/TRIAL_RUNBOOK.md.
#
# It never runs `docker compose down -v`: that would destroy the PostgreSQL
# volume and the encrypted Bybit credential vault. Losing the vault master key
# makes stored credentials unrecoverable.
#
# Environment overrides:
#   BOTDCA_APP_DIR      release root        (default ~/apps/botdca)
#   BOTDCA_COMPOSE_PROJECT  Compose project name (default botdca)
#   BOTDCA_SECRETS_DIR  host secret files   (default: autodetected, see below)
#   BOTDCA_REPO_URL     git remote          (default this repository)
#   BOTDCA_API_ORIGIN   health/API base     (default http://127.0.0.1:8000)
#   BOTDCA_ALLOW_ARMED  set to 1 to deploy with activation flags already on
#
set -euo pipefail

REF="${1:-main}"
APP_DIR="${BOTDCA_APP_DIR:-$HOME/apps/botdca}"
REPO_URL="${BOTDCA_REPO_URL:-https://github.com/baskarajati/botDCA.git}"
API_ORIGIN="${BOTDCA_API_ORIGIN:-http://127.0.0.1:8000}"

MIRROR="$APP_DIR/repo"
RELEASES="$APP_DIR/releases"
CURRENT="$APP_DIR/current"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NEW="$RELEASES/$STAMP"

# Where the host keeps the Compose secret files. A release-directory deployment
# keeps them beside the releases in $APP_DIR/shared/secrets; the runbook example
# uses /etc/botdca/secrets. Autodetect rather than assuming either.
SECRET_NAMES=(credential_key database_password database_url operator_token)
if [ -n "${BOTDCA_SECRETS_DIR:-}" ]; then
  SECRETS_DIR="$BOTDCA_SECRETS_DIR"
else
  SECRETS_DIR=/etc/botdca/secrets
  for candidate in "$APP_DIR/shared/secrets" /etc/botdca/secrets; do
    if [ -r "$candidate/credential_key" ]; then SECRETS_DIR="$candidate"; break; fi
  done
fi

# Compose derives its project name from the working directory unless told
# otherwise. Releases live in timestamped directories, so without an explicit
# project name every deploy would create a SEPARATE stack with its own empty
# volumes rather than updating the existing one - losing the database and the
# encrypted credential vault. Pin it.
COMPOSE_PROJECT="${BOTDCA_COMPOSE_PROJECT:-botdca}"
COMPOSE=(docker compose -p "$COMPOSE_PROJECT" -f docker-compose.yml -f docker-compose.vps.yml)
ACTIVATION_FLAGS=(BOT_LIVE_TRADING BOT_START_LIVE_WORKER BOT_MAINNET_PREFLIGHT_APPROVED)

log() { printf '    %s\n' "$*"; }
fatal() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

case "$REF" in
  -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
esac

echo "==> botDCA deploy $STAMP (ref $REF)"

# -- preconditions ---------------------------------------------------------

command -v git >/dev/null || fatal "git is not installed"
command -v docker >/dev/null || fatal "docker is not installed"
docker compose version >/dev/null 2>&1 || fatal "docker compose v2 is required"

log "compose project:    $COMPOSE_PROJECT"
log "secrets directory:  $SECRETS_DIR"
if ! docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
     2>/dev/null | grep -q .; then
  log "NOTE: no existing '$COMPOSE_PROJECT' containers were found."
  log "      This will create a NEW stack with empty volumes. If you meant to"
  log "      update an existing deployment, stop and set BOTDCA_COMPOSE_PROJECT"
  log "      to its project name (docker compose ls)."
fi
missing=()
for name in "${SECRET_NAMES[@]}"; do
  [ -r "$SECRETS_DIR/$name" ] || missing+=("$name")
done
if [ "${#missing[@]}" -gt 0 ]; then
  {
    echo "FATAL: $SECRETS_DIR is missing or cannot read: ${missing[*]}"
    echo "       Set BOTDCA_SECRETS_DIR to the directory holding the Compose secret files."
    echo "       To find it on a running deployment, inspect the api container's"
    echo "       mount sources: docker inspect \$(docker ps -qf name=api)"
  } >&2
  exit 1
fi

# -- fetch the exact commit ------------------------------------------------

mkdir -p "$RELEASES"
if [ -d "$MIRROR/.git" ]; then
  git -C "$MIRROR" remote set-url origin "$REPO_URL"
  git -C "$MIRROR" fetch --quiet --prune origin
else
  git clone --quiet "$REPO_URL" "$MIRROR"
fi

COMMIT="$(git -C "$MIRROR" rev-parse --verify "origin/$REF^{commit}" 2>/dev/null \
  || git -C "$MIRROR" rev-parse --verify "$REF^{commit}" 2>/dev/null)" \
  || fatal "cannot resolve ref $REF"

mkdir -p "$NEW"
git -C "$MIRROR" archive "$COMMIT" | tar -x -C "$NEW"
printf '%s\n' "$COMMIT" > "$NEW/.deployed-commit"
log "deploying $(git -C "$MIRROR" --no-pager log -1 --format='%h %s' "$COMMIT")"

# -- configuration ---------------------------------------------------------

if [ -f "$CURRENT/.env" ]; then
  cp "$CURRENT/.env" "$NEW/.env"
  log "carried forward the existing .env"
else
  cp "$NEW/.env.example" "$NEW/.env"
  log "WARNING: no previous .env; seeded from .env.example - review it before arming"
fi

# Add any setting this release introduced, using the documented default.
# Existing values are never overwritten.
added=0
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue ;; esac
  key="${line%%=*}"
  if ! grep -q "^${key}=" "$NEW/.env"; then
    printf '%s\n' "$line" >> "$NEW/.env"
    log "+ new setting $line"
    added=$((added + 1))
  fi
done < "$NEW/.env.example"
[ "$added" -eq 0 ] && log "no new settings in this release"

# -- safety gates ----------------------------------------------------------

for flag in "${ACTIVATION_FLAGS[@]}"; do
  if grep -qiE "^${flag}=(true|1|yes)" "$NEW/.env"; then
    if [ "${BOTDCA_ALLOW_ARMED:-0}" = "1" ]; then
      log "WARNING: $flag is enabled and BOTDCA_ALLOW_ARMED=1 was set"
    else
      fatal "$flag is enabled. Deploy in preview, then arm separately (see docs/TRIAL_RUNBOOK.md).
       Override only if you know why: BOTDCA_ALLOW_ARMED=1 $0 $REF"
    fi
  fi
done

# The portfolio margin cap binds the whole bot across every symbol, so it must
# stay within the operator's trial equity reference or live startup is blocked.
equity="$(sed -n 's/^BOT_TRIAL_EQUITY_USDT=//p' "$NEW/.env" | tail -1)"
cap="$(sed -n 's/^BOT_MAX_TOTAL_BOT_MARGIN_USDT=//p' "$NEW/.env" | tail -1)"
if [ -n "$equity" ] && [ -n "$cap" ] && awk "BEGIN{exit !($cap > $equity)}" 2>/dev/null; then
  log "WARNING: BOT_MAX_TOTAL_BOT_MARGIN_USDT=$cap exceeds BOT_TRIAL_EQUITY_USDT=$equity."
  log "         The 'Live configuration' readiness check will fail until you lower it."
fi

# -- build and start -------------------------------------------------------

cd "$NEW"
export BOTDCA_CREDENTIAL_KEY_FILE="$SECRETS_DIR/credential_key"
export BOTDCA_DATABASE_PASSWORD_FILE="$SECRETS_DIR/database_password"
export BOTDCA_DATABASE_URL_FILE="$SECRETS_DIR/database_url"
export BOTDCA_OPERATOR_TOKEN_FILE="$SECRETS_DIR/operator_token"
export BOTDCA_UID="${BOTDCA_UID:-$(id -u)}"
export BOTDCA_GID="${BOTDCA_GID:-$(id -g)}"

PREVIOUS="$(readlink -f "$CURRENT" 2>/dev/null || true)"

"${COMPOSE[@]}" config -q
"${COMPOSE[@]}" up -d --build

# -- verify ----------------------------------------------------------------
# The additive schema migration runs automatically during startup.

echo "==> waiting for the API to become healthy"
healthy=0
for _ in $(seq 1 45); do
  if curl -fsS "$API_ORIGIN/health" >/dev/null 2>&1; then healthy=1; break; fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  echo "FATAL: the API did not become healthy; $CURRENT was NOT moved." >&2
  "${COMPOSE[@]}" ps >&2 || true
  "${COMPOSE[@]}" logs --tail 60 api >&2 || true
  if [ -n "$PREVIOUS" ]; then
    echo "       current still points at $PREVIOUS" >&2
    echo "       Restore it with: cd $PREVIOUS && ${COMPOSE[*]} up -d --build" >&2
  fi
  exit 1
fi

# Publish the release only now that the API answers.
ln -sfn "$NEW" "$CURRENT"

token="$(cat "$SECRETS_DIR/operator_token")"
api() { curl -fsS -H "X-Operator-Token: $token" "$API_ORIGIN$1"; }

echo "--- health ---"
curl -fsS "$API_ORIGIN/health"; echo
echo "--- strategy version ---"
api /api/v1/strategy/versions | python3 -c '
import json, sys
d = json.load(sys.stdin)
v = d["versions"][d["configured_version_id"]]
print(v["version_id"], "|", v["status"], "| live <= DCA%d" % v["max_live_dca_level"])
print("research-only (never traded live):", v["research_dca_triggers"])'
echo "--- portfolio guards ---"
api /api/v1/portfolio | python3 -c '
import json, sys
print(json.dumps(json.load(sys.stdin)["guards"], indent=2))'
echo "--- activation gate ---"
api /api/v1/configuration/activation | python3 -c '
import json, sys
g = json.load(sys.stdin)["gate"]
print(g["status"], "| allowed:", g["allowed"])
for e in g["errors"]:
    print("  -", e)'
echo "--- readiness ---"
api /api/v1/operations | python3 -c '
import json, sys
for c in json.load(sys.stdin)["readiness"]:
    print("PASS" if c["passed"] else "FAIL", c["label"])'

echo
echo "==> deployed $STAMP at $COMMIT"
echo "    Live trading, worker startup and mainnet preflight remain OFF."
if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$NEW" ]; then
  echo "    Roll back: ln -sfn $PREVIOUS $CURRENT && cd $PREVIOUS && ${COMPOSE[*]} up -d --build"
fi
