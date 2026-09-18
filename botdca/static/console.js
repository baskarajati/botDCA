"use strict";

const $ = (id) => document.getElementById(id);
const setText = (id, value) => {
  $(id).textContent = value;
};
const formatNumber = (value, digits = 4) =>
  value === null || value === undefined || !Number.isFinite(Number(value))
    ? "—"
    : Number(value).toLocaleString(undefined, {
        maximumFractionDigits: digits,
      });
const formatMoney = (value) =>
  value === null || value === undefined
    ? "—"
    : `${formatNumber(value, 2)} USDT`;
const formatPercent = (value, digits = 2) =>
  value === null || value === undefined
    ? "—"
    : `${formatNumber(value, digits)}%`;
const formatBoolean = (value) => (value ? "Enabled" : "Disabled");
const formatConfigured = (value) => (value ? "Configured" : "Not configured");
const formatCredentialSource = (value) =>
  ({
    "encrypted-vault": "Encrypted VPS vault",
    environment: "Process environment",
    none: "Not configured",
  })[value] || "Unknown";
const formatTime = (value) =>
  value
    ? new Date(value).toLocaleString([], {
        dateStyle: "medium",
        timeStyle: "medium",
      })
    : "—";
const formatUtc = (milliseconds) =>
  milliseconds
    ? new Date(milliseconds).toISOString().replace("T", " ").slice(0, 19) +
      " UTC"
    : "—";

let operatorToken = "";
let snapshot = null;
let busy = false;
let polling = false;
let generation = 0;
let lastSuccess = 0;
let strategySlotsDirty = false;
let symbolsLoaded = false;
let selectedCloseSymbol = "";

const stateNames = {
  paused: "Re-entry paused",
  idle: "Entry enabled",
  active: "Managing basket",
  closing: "Closing",
  error: "Error",
};

const pages = {
  overview: {
    path: "/",
    eyebrow: "Operator overview",
    title: "Operate one trial basket",
    description:
      "Monitor exchange state, control re-entry, and confirm the trial is ready.",
  },
  configuration: {
    path: "/configuration",
    eyebrow: "System configuration",
    title: "Configure the trial console",
    description:
      "Connect the operator session and inspect every server-backed strategy, risk, and runtime setting.",
  },
  journal: {
    path: "/journal",
    eyebrow: "Evidence and audit",
    title: "Review execution records",
    description:
      "Verify exchange fills, export recent records, and review operator actions.",
  },
};

function pageFromPath(pathname) {
  const normalized = pathname !== "/" ? pathname.replace(/\/$/, "") : pathname;
  return (
    Object.entries(pages).find(([, page]) => page.path === normalized)?.[0] ||
    "overview"
  );
}

function showPage(pageName, { focus = false } = {}) {
  const page = pages[pageName] || pages.overview;
  document.querySelectorAll("[data-page-view]").forEach((view) => {
    view.hidden = view.dataset.pageView !== pageName;
  });
  document.querySelectorAll("nav [data-route]").forEach((link) => {
    if (link.dataset.route === pageName)
      link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  setText("page-eyebrow", page.eyebrow);
  setText("page-title", page.title);
  setText("page-description", page.description);
  document.title = `${page.title} · botDCA`;
  document.body.classList.remove("route-pending");
  if (focus) {
    window.scrollTo({ top: 0, behavior: "auto" });
    $("page-title").focus({ preventScroll: true });
  }
}

function navigate(pageName) {
  const page = pages[pageName] || pages.overview;
  if (window.location.pathname !== page.path)
    history.pushState({}, "", page.path);
  showPage(pageName, { focus: true });
}

function setStatus(id, message, kind = "") {
  setText(id, message);
  $(id).className = `status-text${kind ? ` ${kind}` : ""}`;
}

function showError(message = "") {
  setText("error", message);
  $("error").hidden = !message;
}

function showNotice(message = "") {
  setText("notice", message);
  $("notice").hidden = !message;
}

function setSessionConnected(connected, authenticationRequired = true) {
  $("connect-form").hidden = connected;
  $("session-active").hidden = !connected;
  if (connected && !authenticationRequired) {
    $("session-active").querySelector("strong").textContent =
      "Authentication not required";
    $("session-active").querySelector("span").textContent =
      "Mutating controls remain unavailable unless the server has an operator token.";
  } else if (connected) {
    $("session-active").querySelector("strong").textContent =
      "Console connected";
    $("session-active").querySelector("span").textContent =
      "The token is held only in page memory.";
  }
}

function updateControls() {
  const fresh = snapshot && Date.now() - lastSuccess < 20000;
  const position = snapshot?.account?.position;
  const hasPosition =
    snapshot &&
    (snapshot.mode === "preview"
      ? snapshot.bot.position_qty > 0
      : Number(position?.size || 0) > 0);
  $("resume").disabled = busy || !fresh || !snapshot?.can_resume;
  $("pause").disabled =
    busy ||
    !fresh ||
    !snapshot?.configuration?.console?.operator_token_configured;
  $("close").disabled =
    busy ||
    !fresh ||
    !snapshot?.configuration?.console?.operator_token_configured ||
    !hasPosition;
  $("export").disabled = busy || !fresh || !snapshot?.journal?.available;
  $("credential-submit").disabled =
    busy ||
    !fresh ||
    !snapshot?.configuration?.exchange?.credential_vault_enabled ||
    snapshot?.worker?.running;
  $("strategy-slots-save").disabled =
    busy ||
    !fresh ||
    !snapshot?.configuration?.console?.operator_token_configured ||
    Object.values(snapshot?.workers || {}).some((worker) => worker.running);
  document.querySelectorAll("[data-basket-action]").forEach((button) => {
    const action = button.dataset.basketAction;
    const symbol = button.dataset.symbol;
    const position = snapshot?.account?.positions?.[symbol];
    button.disabled =
      busy ||
      !fresh ||
      (action === "resume" && !snapshot?.can_resume) ||
      (action === "manual-close" && Number(position?.size || 0) <= 0);
  });
}

async function request(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {
      ...options,
      cache: "no-store",
      signal: controller.signal,
      headers: { "X-Operator-Token": operatorToken, ...options.headers },
    });
    if (!response.ok) {
      let detail = `Request failed (${response.status}).`;
      try {
        detail = (await response.json()).detail || detail;
      } catch {}
      const requestError = new Error(detail);
      requestError.status = response.status;
      throw requestError;
    }
    return response;
  } finally {
    clearTimeout(timeout);
  }
}

function emptyRow(message) {
  const row = document.createElement("tr");
  const cell = document.createElement("td");
  cell.colSpan = 6;
  cell.className = "empty-row";
  cell.textContent = message;
  row.append(cell);
  return row;
}

function renderReadiness(status) {
  $("checks").replaceChildren();
  status.readiness.forEach((check) => {
    const item = document.createElement("article");
    const icon = document.createElement("span");
    const body = document.createElement("div");
    const label = document.createElement("div");
    const detail = document.createElement("div");
    item.className = "check-item";
    icon.className = `check-icon${check.passed ? " pass" : ""}`;
    icon.textContent = check.passed ? "✓" : "!";
    icon.setAttribute("aria-label", check.passed ? "Passed" : "Not ready");
    label.className = "check-label";
    label.textContent = check.label;
    detail.className = "check-detail";
    detail.textContent = check.detail;
    body.append(label, detail);
    item.append(icon, body);
    $("checks").append(item);
  });

  const liveReady = status.mode !== "preview" && status.can_resume;
  setText(
    "readiness-state",
    liveReady ? "Ready for entry controls" : "Not ready for live entries",
  );
  $("readiness-state").className =
    `readiness-state ${liveReady ? "positive" : "caution"}`;
  setText(
    "readiness-summary",
    status.mode === "preview"
      ? "Preview controls can change local state, but they cannot place orders. Live activation stays outside this console."
      : liveReady
        ? "The configured checks currently pass. Continue to monitor exchange reconciliation before acting."
        : "New entries remain blocked until every required check passes.",
  );
}

function renderConfiguration(configuration) {
  const strategy = configuration.strategy;
  const limits = configuration.risk_limits;
  const exchange = configuration.exchange;
  const runtime = configuration.runtime;
  const consoleConfiguration = configuration.console;

  setText("fingerprint", configuration.fingerprint);
  setText("config-exchange-environment", configuration.exchange_environment);
  setText("config-api-key", formatConfigured(exchange.api_key_configured));
  setText(
    "config-api-secret",
    formatConfigured(exchange.api_secret_configured),
  );
  setText("config-account-mode", exchange.account_mode);
  setText(
    "config-credential-source",
    formatCredentialSource(exchange.credential_source),
  );
  setText(
    "config-credential-vault",
    !exchange.credential_vault_enabled
      ? "Vault key not mounted"
      : exchange.credential_vault_persisted
        ? "Encrypted credentials stored"
        : "Ready for credentials",
  );
  const validation = exchange.credential_validation || {};
  setText(
    "credential-validation-summary",
    validation.validated_at
      ? `Validated ${formatTime(validation.validated_at)} · fingerprint ${validation.key_fingerprint} · ${validation.ip_binding_count} IP ${validation.ip_binding_count === 1 ? "binding" : "bindings"}.`
      : !exchange.credential_vault_enabled
        ? "Mount the VPS vault key before submitting credentials."
        : "Vault ready. No validated credentials are stored yet.",
  );
  const enabledSlots = (configuration.strategy_slots || []).filter(
    (slot) => slot.enabled,
  );
  setText(
    "config-symbol",
    enabledSlots.length
      ? `${enabledSlots.length} · ${enabledSlots.map((slot) => slot.symbol).join(", ")}`
      : strategy.symbol,
  );
  setText("config-direction", strategy.direction.replaceAll("-", " "));
  setText("config-leverage", `${strategy.leverage}×`);
  setText(
    "config-base-margin",
    enabledSlots.length
      ? enabledSlots
          .map((slot) => `${slot.symbol} ${formatMoney(slot.base_margin_usdt)}`)
          .join(" · ")
      : formatMoney(strategy.base_margin_usdt),
  );
  setText("config-tp", formatPercent(strategy.tp_percent, 3));
  setText("config-reentry", `${configuration.reentry_delay_seconds} seconds`);
  setText("config-max-dca", `${limits.max_dca_level} levels`);
  setText(
    "config-trial-equity",
    formatMoney(configuration.trial_equity_reference_usdt),
  );
  setText("config-margin-cap", formatMoney(limits.max_strategy_margin_usdt));
  setText(
    "config-equity-reserve",
    formatPercent(limits.min_available_equity_ratio * 100, 0),
  );
  setText("config-min-balance", formatMoney(limits.min_available_balance_usdt));
  setText("config-app-environment", runtime.environment);
  setText("config-live-trading", formatBoolean(runtime.live_trading));
  setText("config-worker-start", formatBoolean(runtime.start_live_worker));
  setText(
    "config-worker-interval",
    `${configuration.worker_interval_seconds} seconds`,
  );
  setText(
    "config-mainnet-preflight",
    runtime.mainnet_preflight_approved ? "Approved" : "Not approved",
  );
  setText("config-database", runtime.database_backend);
  setText(
    "config-allowed-hosts",
    consoleConfiguration.allowed_hosts.join(", "),
  );

  $("ladder").replaceChildren();
  strategy.dca_steps.slice(0, limits.max_dca_level).forEach((step, index) => {
    const item = document.createElement("div");
    const label = document.createElement("span");
    const value = document.createElement("strong");
    item.className = "ladder-step";
    label.textContent = `DCA ${index + 1}`;
    value.textContent = `${formatPercent(step.drop_percent_from_average)} drop · ${formatNumber(step.size_multiplier_from_previous, 3)}× size`;
    item.append(label, value);
    $("ladder").append(item);
  });

  if (!strategySlotsDirty) renderStrategySlotEditor(configuration);
}

function strategySlotValues() {
  return [...document.querySelectorAll(".strategy-slot-card")].map((card) => ({
    slot: Number(card.dataset.slot),
    enabled: card.querySelector("[data-slot-enabled]").checked,
    symbol: card.querySelector("[data-slot-symbol]").value.trim().toUpperCase(),
    initial_margin_usdt: Number(
      card.querySelector("[data-slot-margin]").value,
    ),
  }));
}

function forecastRowsForMargin(baseMargin, strategy, maxDcaLevel) {
  if (!Number.isFinite(baseMargin) || baseMargin <= 0) return [];
  const rows = [
    {
      level: 0,
      drop_percent: 0,
      size_multiplier: 1,
      incremental_margin_usdt: baseMargin,
      cumulative_margin_usdt: baseMargin,
    },
  ];
  const leverage = Number(strategy.leverage);
  let totalQty = (baseMargin * leverage) / 100;
  let totalNotional = baseMargin * leverage;
  let lastQty = totalQty;
  let cumulative = baseMargin;
  strategy.dca_steps.slice(0, maxDcaLevel).forEach((step, index) => {
    const average = totalNotional / totalQty;
    const price = average * (1 - step.drop_percent_from_average / 100);
    const qty = lastQty * step.size_multiplier_from_previous;
    const incremental = (price * qty) / leverage;
    cumulative += incremental;
    totalQty += qty;
    totalNotional += price * qty;
    lastQty = qty;
    rows.push({
      level: index + 1,
      drop_percent: step.drop_percent_from_average,
      size_multiplier: step.size_multiplier_from_previous,
      incremental_margin_usdt: incremental,
      cumulative_margin_usdt: cumulative,
    });
  });
  return rows;
}

function renderStrategyForecast() {
  const body = $("strategy-forecast");
  body.replaceChildren();
  const strategy = snapshot?.configuration?.strategy;
  const limits = snapshot?.configuration?.risk_limits;
  if (!strategy || !limits) return;
  let combined = 0;
  strategySlotValues()
    .filter((slot) => slot.enabled)
    .forEach((slot) => {
      const rows = forecastRowsForMargin(
        slot.initial_margin_usdt,
        strategy,
        limits.max_dca_level,
      );
      if (rows.length) combined += rows.at(-1).cumulative_margin_usdt;
      rows.forEach((forecast) => {
        const row = document.createElement("tr");
        [
          slot.symbol || `Slot ${slot.slot}`,
          forecast.level === 0 ? "DCA0 · Initial" : `DCA${forecast.level}`,
          forecast.level === 0
            ? "Entry"
            : formatPercent(forecast.drop_percent),
          `${formatNumber(forecast.size_multiplier, 3)}×`,
          formatMoney(forecast.incremental_margin_usdt),
          formatMoney(forecast.cumulative_margin_usdt),
        ].forEach((value) => {
          const cell = document.createElement("td");
          cell.textContent = value;
          row.append(cell);
        });
        body.append(row);
      });
    });
  if (!body.children.length) body.append(emptyRow("Enable at least one coin to see its forecast."));
  setText("combined-forecast", `Combined full ladder · ${formatMoney(combined)}`);
  const reference = Number(snapshot?.configuration?.trial_equity_reference_usdt);
  $("combined-forecast").className =
    `forecast-total${Number.isFinite(reference) && combined > reference ? " caution" : ""}`;
}

function renderStrategySlotEditor(configuration) {
  const container = $("strategy-slot-cards");
  container.replaceChildren();
  (configuration.strategy_slots || []).forEach((slot) => {
    const card = document.createElement("fieldset");
    card.className = "strategy-slot-card";
    card.dataset.slot = slot.slot;
    const legend = document.createElement("legend");
    legend.textContent = `Strategy slot ${slot.slot}`;

    const enableLabel = document.createElement("label");
    enableLabel.className = "slot-enable";
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.checked = slot.enabled;
    enabled.dataset.slotEnabled = "";
    enableLabel.append(enabled, document.createTextNode(" Enable this coin"));

    const symbolLabel = document.createElement("label");
    symbolLabel.htmlFor = `slot-symbol-${slot.slot}`;
    symbolLabel.textContent = "Coin";
    const symbol = document.createElement("input");
    symbol.id = symbolLabel.htmlFor;
    symbol.value = slot.symbol;
    symbol.setAttribute("list", "bybit-symbols");
    symbol.autocomplete = "off";
    symbol.spellcheck = false;
    symbol.dataset.slotSymbol = "";

    const marginLabel = document.createElement("label");
    marginLabel.htmlFor = `slot-margin-${slot.slot}`;
    marginLabel.textContent = "Initial margin (USDT)";
    const margin = document.createElement("input");
    margin.id = marginLabel.htmlFor;
    margin.type = "number";
    margin.min = "0.01";
    margin.step = "0.01";
    margin.inputMode = "decimal";
    margin.value = slot.base_margin_usdt;
    margin.dataset.slotMargin = "";

    const hint = document.createElement("p");
    hint.className = "slot-hint";
    hint.textContent = "DCA0 margin. Later levels scale from this amount.";
    card.append(legend, enableLabel, symbolLabel, symbol, marginLabel, margin, hint);
    container.append(card);
  });
  renderStrategyForecast();
}

function renderJournal(journal) {
  setText(
    "fill-count",
    journal.available
      ? `${journal.execution_count} ${journal.execution_count === 1 ? "record" : "records"}`
      : "Unavailable",
  );
  $("fills").replaceChildren();
  if (!journal.available || !journal.executions.length) {
    $("fills").append(
      emptyRow(
        journal.available
          ? "No exchange fills recorded yet. Verified fills will appear here after the worker connects."
          : journal.error,
      ),
    );
  }
  journal.executions.forEach((fill) => {
    const row = document.createElement("tr");
    [
      formatUtc(fill.execution_time_ms),
      fill.side,
      formatNumber(fill.qty, 6),
      formatNumber(fill.price),
      formatNumber(fill.fee, 6),
      fill.order_id,
    ].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      cell.title = value;
      row.append(cell);
    });
    $("fills").append(row);
  });

  $("activity").replaceChildren();
  if (!journal.events.length) {
    const empty = document.createElement("p");
    empty.className = "empty-copy";
    empty.textContent = journal.available
      ? "No operator action recorded in this session."
      : "Operator activity is unavailable while the journal is offline.";
    $("activity").append(empty);
    return;
  }
  journal.events.slice(0, 8).forEach((event) => {
    const item = document.createElement("div");
    const label = document.createElement("strong");
    const time = document.createElement("time");
    item.className = "activity-item";
    label.textContent = event.event_type.replaceAll("_", " ").toLowerCase();
    time.textContent = `${event.occurred_at.replace("T", " ").slice(0, 19)} UTC`;
    item.append(label, time);
    $("activity").append(item);
  });
}


function renderPortfolio(status) {
  const portfolio = status.portfolio;
  const guards = status.portfolio_guards;
  if (!portfolio || !guards) {
    setStatus("portfolio-state", "Unavailable", "caution");
    return;
  }
  const margin = portfolio.total_bot_margin_usdt || 0;
  const cap = guards.max_total_bot_margin_usdt || 0;
  const used = cap > 0 ? margin / cap : 0;
  setText("portfolio-margin", formatMoney(margin));
  setText("portfolio-margin-cap", formatMoney(cap));
  setText("portfolio-notional", formatMoney(portfolio.total_bot_notional_usdt));
  setText(
    "portfolio-deep",
    `${portfolio.deep_basket_count} of ${guards.max_simultaneous_deep_baskets} at DCA${guards.deep_dca_level}+`,
  );
  setText("portfolio-upl", formatMoney(portfolio.total_unrealized_pnl_usdt));
  setText(
    "portfolio-available",
    portfolio.available_balance_usdt === null
      ? "Account unavailable"
      : `${formatMoney(portfolio.available_balance_usdt)} (${formatPercent(
          (portfolio.available_equity_ratio || 0) * 100,
        )})`,
  );
  setStatus(
    "portfolio-state",
    `${formatPercent(used * 100)} of budget committed`,
    used >= 1 ? "failure" : used >= 0.6 ? "caution" : "",
  );

  const workers = status.workers || {};
  const intervention = Object.entries(workers)
    .filter(([, worker]) => worker.manual_intervention_required)
    .map(([symbol, worker]) => `${symbol} (${worker.last_sync_status})`);
  const banner = $("portfolio-intervention");
  banner.hidden = intervention.length === 0;
  banner.textContent = intervention.length
    ? `Manual intervention required: ${intervention.join(", ")}. Take profit and reduce-only close remain available.`
    : "";
}

function renderStrategyVersion(status) {
  const bot = status.bot;
  const versions = status.strategy_versions || {};
  const version =
    versions[bot.strategy_version_id] ||
    versions[status.configuration?.strategy?.strategy_version_id] ||
    Object.values(versions)[0];
  if (!version) return;

  setText("strategy-version-id", version.version_id);
  setText(
    "strategy-direction",
    `${version.direction} · ${version.margin_mode} ${version.leverage}x`,
  );
  setText("strategy-tp", `+${formatPercent(version.take_profit_percent)}`);
  setText("strategy-multiplier", `x${formatNumber(version.dca_size_multiplier, 2)}`);
  setText(
    "strategy-live-ladder",
    `DCA1-DCA${version.max_live_dca_level} (${version.live_dca_triggers
      .map((trigger) => `${trigger}%`)
      .join(", ")})`,
  );
  setText("strategy-version-status", version.experimental ? "EXPERIMENTAL" : version.status);
  setText("strategy-version-summary", version.summary || "");
  setText(
    "strategy-activation",
    status.activation ? status.activation.status : "unknown",
  );
  setText(
    "strategy-research-note",
    version.research_dca_triggers.length
      ? `Research-only levels DCA${version.max_live_dca_level + 1}-DCA${version.max_research_dca_level} (${version.research_dca_triggers
          .map((trigger) => `${trigger}%`)
          .join(", ")}) are never traded live. They exist for forecasting and stress testing only.`
      : "",
  );
}

function renderAlerts(status) {
  const body = $("alerts-rows");
  if (!body) return;
  const alerts = status.journal?.alerts || [];
  body.replaceChildren();
  if (!alerts.length) {
    body.append(emptyRow("No alerts recorded."));
    setStatus("alerts-state", "No alerts", "");
    return;
  }
  alerts.forEach((alert) => {
    const row = document.createElement("tr");
    [
      formatTime(alert.occurred_at),
      alert.severity,
      alert.symbol,
      alert.condition,
      alert.message,
    ].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    });
    if (alert.severity === "critical") row.className = "row-danger";
    body.append(row);
  });
  const critical = status.open_critical_alerts || [];
  setStatus(
    "alerts-state",
    critical.length ? `${critical.length} open critical` : `${alerts.length} recorded`,
    critical.length ? "failure" : "",
  );
}

function render(status) {
  snapshot = status;
  lastSuccess = Date.now();
  const bot = status.bot;
  const configuration = status.configuration;
  const account = status.account.account;
  const position = status.account.position;
  const modeLabel =
    status.mode === "preview"
      ? "Preview · no orders"
      : status.mode === "testnet"
        ? "Testnet trading"
        : "Mainnet trading";
  setStatus(
    "mode",
    modeLabel,
    status.mode === "mainnet"
      ? "failure"
      : status.mode === "testnet"
        ? "caution"
        : "",
  );
  setText("basket-mode", modeLabel);
  setText("last-refresh", formatTime(status.observed_at));
  setText("symbol", bot.symbol);
  setText("state", stateNames[bot.state] || bot.state);
  setText(
    "basket-description",
    bot.state === "paused"
      ? "Re-entry is paused. Position protection continues while a basket is open."
      : bot.state === "idle"
        ? "Entries are enabled. The worker may open the next eligible basket."
        : "The worker is managing this basket from reconciled exchange state.",
  );
  setText("average", formatNumber(bot.average_entry));
  setText(
    "quantity",
    bot.position_qty
      ? `${formatNumber(bot.position_qty, 6)} ${bot.symbol}`
      : "No open basket",
  );
  setText(
    "position-upl",
    position ? formatMoney(position.unrealized_pnl) : "—",
  );
  $("position-upl").className =
    position?.unrealized_pnl < 0
      ? "negative"
      : position?.unrealized_pnl > 0
        ? "positive"
        : "";
  setText("mark-price", position ? formatNumber(position.mark_price) : "—");
  setText("tp", formatNumber(bot.tp_price));
  setText("next-dca", formatNumber(bot.next_dca_price));
  setText("depth", `${bot.dca_level} / ${bot.max_dca_level}`);
  setText("leverage", `${bot.leverage}×`);
  setText(
    "trial-equity",
    formatMoney(configuration.trial_equity_reference_usdt),
  );
  setText("equity", account ? formatMoney(account.total_equity_usd) : "—");
  setText(
    "available",
    account ? formatMoney(account.total_available_balance_usd) : "—",
  );
  setText(
    "margin",
    `${formatMoney(bot.committed_margin_usdt)} / ${formatMoney(bot.max_strategy_margin_usdt)}`,
  );
  setText(
    "account-state",
    account
      ? "Connected · read only"
      : status.account.error || "Credentials not configured",
  );
  setText("account-source", configuration.exchange.account_mode);
  setText(
    "liquidation",
    position?.liquidation_price
      ? formatNumber(position.liquidation_price)
      : "Unavailable",
  );
  setText(
    "risk-message",
    bot.position_qty
      ? status.account.dca_risk?.reason ||
          bot.dca_blocked_reason ||
          "Within configured limits; exchange checks still apply."
      : "No next DCA while the strategy is flat.",
  );
  setText("worker", status.worker.running ? "Running" : "Stopped");
  setText(
    "stream",
    status.worker.stream_connected ? "Connected" : "Disconnected",
  );
  setText("sync", status.worker.last_sync_status || "No successful sync");
  setText("environment", configuration.exchange_environment);
  setStatus(
    "worker-error",
    status.worker.last_error || "No worker error",
    status.worker.last_error ? "failure" : "positive",
  );

  renderReadiness(status);
  renderConfiguration(configuration);
  renderJournal(status.journal);
  renderAdditionalBaskets(status);
  renderPortfolio(status);
  renderStrategyVersion(status);
  renderAlerts(status);
  showNotice(
    status.mode === "preview"
      ? "Live trading is disabled. Entry controls change local state only; this preview does not simulate fills."
      : status.mode === "testnet"
        ? "Testnet orders are enabled. Verify fills, reconnects, and closure before considering a mainnet trial."
        : "Mainnet orders are enabled. Confirm the account, position, and risk limits before enabling entries.",
  );
  updateControls();
}

function renderAdditionalBaskets(status) {
  const bots = status.bots || [];
  const additional = bots.slice(1);
  $("additional-baskets").hidden = additional.length === 0;
  const list = $("additional-basket-list");
  list.replaceChildren();
  additional.forEach((bot) => {
    const position = status.account.positions?.[bot.symbol];
    const worker = status.workers?.[bot.symbol];
    const card = document.createElement("article");
    card.className = "additional-basket-card";
    const heading = document.createElement("div");
    heading.className = "additional-basket-heading";
    const title = document.createElement("h3");
    title.textContent = bot.symbol;
    const state = document.createElement("span");
    state.className = "state-label";
    state.textContent = stateNames[bot.state] || bot.state;
    heading.append(title, state);

    const metrics = document.createElement("dl");
    metrics.className = "mini-basket-metrics";
    [
      ["Position", position?.size ? formatNumber(position.size, 6) : "Flat"],
      [
        "DCA depth",
        bot.max_dca_reached
          ? `${bot.dca_level} / ${bot.max_dca_level} · MAX REACHED`
          : `${bot.dca_level} / ${bot.max_dca_level}`,
      ],
      [
        "Initial allocation",
        bot.sizing_mode === "fixed_base_quantity"
          ? `${formatNumber(bot.sizing_value, 6)} ${bot.symbol.replace("USDT", "")}`
          : formatMoney(bot.sizing_value),
      ],
      ["Protection", worker?.protection_status || "—"],
      ["Worker", worker?.running ? "Running" : "Stopped"],
    ].forEach(([label, value]) => {
      const wrapper = document.createElement("div");
      const term = document.createElement("dt");
      const detail = document.createElement("dd");
      term.textContent = label;
      detail.textContent = value;
      wrapper.append(term, detail);
      metrics.append(wrapper);
    });

    const actions = document.createElement("div");
    actions.className = "button-row";
    [
      ["resume", "Enable entries", "button primary"],
      ["pause", "Pause new entries", "button secondary"],
      ["manual-close", "Close position & pause", "button danger-button"],
    ].forEach(([action, label, className]) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = className;
      button.textContent = label;
      button.dataset.basketAction = action;
      button.dataset.symbol = bot.symbol;
      actions.append(button);
    });
    card.append(heading, metrics, actions);
    list.append(card);
  });
}

async function load() {
  if (polling || document.hidden) return;
  polling = true;
  const currentGeneration = generation;
  try {
    const status = await (await request("/api/v1/operations")).json();
    if (currentGeneration === generation) {
      render(status);
      showError();
      setSessionConnected(true, true);
      if (!symbolsLoaded) loadBybitSymbols();
    }
  } catch (loadError) {
    if (currentGeneration !== generation) return;
    showError(
      loadError.name === "AbortError"
        ? "The server request timed out. Values may be stale; wait for a fresh refresh before acting."
        : loadError.message,
    );
    lastSuccess = 0;
    if (loadError.status === 401) {
      operatorToken = "";
      snapshot = null;
      setSessionConnected(false);
      setStatus("mode", "Console locked");
    }
    updateControls();
  } finally {
    polling = false;
  }
}

async function loadBybitSymbols() {
  symbolsLoaded = true;
  try {
    const result = await (
      await request("/api/v1/configuration/bybit-symbols")
    ).json();
    const list = $("bybit-symbols");
    list.replaceChildren();
    result.symbols.forEach((symbol) => {
      const option = document.createElement("option");
      option.value = symbol;
      list.append(option);
    });
  } catch {
    $("strategy-slots-status").textContent =
      "Coin suggestions are temporarily unavailable. You can still type an exact USDT symbol.";
  }
}

async function runAction(name, symbol = "") {
  if (busy) return;
  busy = true;
  updateControls();
  const currentGeneration = generation;
  try {
    const result = await (
      await request(
        symbol
          ? `/api/v1/bots/${encodeURIComponent(symbol)}/${name}`
          : `/api/v1/bot/${name}`,
        { method: "POST" },
      )
    ).json();
    await load();
    if (currentGeneration === generation) {
      showNotice(
        result.status === "close_submitted"
          ? "Close submitted. Keep monitoring the exchange position until it is confirmed flat."
          : name === "pause"
            ? "New entries are paused. Protection for an existing basket continues."
            : snapshot?.mode === "preview"
              ? "Local preview state changed. No exchange order was placed."
              : "Request accepted. Confirm the reconciled state above.",
      );
    }
  } catch (actionError) {
    if (currentGeneration === generation) showError(actionError.message);
  } finally {
    busy = false;
    updateControls();
  }
}

$("connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  operatorToken = $("operator-token").value;
  generation += 1;
  await load();
  if (snapshot) $("operator-token").value = "";
});

$("credential-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  updateControls();
  const apiKeyInput = $("bybit-api-key");
  const apiSecretInput = $("bybit-api-secret");
  const confirmationInput = $("confirm-mainnet");
  const status = $("credential-status");
  status.className = "form-status";
  status.textContent = "Validating read-only account access on Bybit mainnet…";
  try {
    await request("/api/v1/configuration/bybit-credentials", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: apiKeyInput.value,
        api_secret: apiSecretInput.value,
        confirm_mainnet: confirmationInput.value.trim(),
      }),
    });
    status.className = "form-status positive";
    status.textContent =
      "Credentials validated and encrypted. The worker remains stopped and entries remain disabled.";
    await load();
  } catch (credentialError) {
    status.className = "form-status failure";
    status.textContent = credentialError.message;
  } finally {
    apiKeyInput.value = "";
    apiSecretInput.value = "";
    confirmationInput.value = "";
    busy = false;
    updateControls();
  }
});

$("strategy-slot-cards").addEventListener("input", (event) => {
  if (!event.target.closest(".strategy-slot-card")) return;
  strategySlotsDirty = true;
  $("strategy-slot-errors").textContent = "";
  $("strategy-slots-status").textContent = "Unsaved changes.";
  renderStrategyForecast();
});

$("strategy-slots-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  const slots = strategySlotValues();
  const errors = [];
  if (!slots.some((slot) => slot.enabled)) errors.push("Enable at least one coin.");
  const enabledSymbols = slots
    .filter((slot) => slot.enabled)
    .map((slot) => slot.symbol);
  if (new Set(enabledSymbols).size !== enabledSymbols.length)
    errors.push("Choose a different coin for each enabled slot.");
  slots.forEach((slot) => {
    if (!/^[A-Z0-9]{2,24}USDT$/.test(slot.symbol))
      errors.push(`Slot ${slot.slot}: enter a USDT perpetual such as HYPEUSDT.`);
    if (!Number.isFinite(slot.initial_margin_usdt) || slot.initial_margin_usdt <= 0)
      errors.push(`Slot ${slot.slot}: enter an initial margin greater than zero.`);
  });
  if (errors.length) {
    $("strategy-slot-errors").textContent = errors.join(" ");
    $("strategy-slot-errors").focus();
    return;
  }

  busy = true;
  updateControls();
  const status = $("strategy-slots-status");
  status.className = "form-status";
  status.textContent = "Verifying coins on Bybit and saving configuration…";
  try {
    await request("/api/v1/configuration/strategy-slots", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slots }),
    });
    strategySlotsDirty = false;
    status.className = "form-status positive";
    status.textContent =
      "Strategy slots saved. Workers remain stopped and entries remain disabled.";
    await load();
  } catch (slotError) {
    status.className = "form-status failure";
    status.textContent = slotError.message;
  } finally {
    busy = false;
    updateControls();
  }
});

$("resume").addEventListener("click", () => runAction("resume"));
$("pause").addEventListener("click", () => runAction("pause"));
$("close").addEventListener("click", () => {
  selectedCloseSymbol = snapshot?.bot?.symbol || "";
  $("confirm-symbol").value = "";
  $("confirm-close").disabled = true;
  $("close-dialog").returnValue = "";
  $("close-dialog").showModal();
});
$("confirm-symbol").addEventListener("input", () => {
  $("confirm-close").disabled =
    $("confirm-symbol").value.trim() !== selectedCloseSymbol;
});
$("close-dialog").addEventListener("close", () => {
  if ($("close-dialog").returnValue === "confirm")
    runAction("manual-close", selectedCloseSymbol === snapshot?.bot?.symbol ? "" : selectedCloseSymbol);
});

$("additional-basket-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-basket-action]");
  if (!button) return;
  if (button.dataset.basketAction === "manual-close") {
    selectedCloseSymbol = button.dataset.symbol;
    $("confirm-symbol").value = "";
    $("confirm-close").disabled = true;
    $("close-dialog").returnValue = "";
    $("close-dialog").showModal();
    return;
  }
  runAction(button.dataset.basketAction, button.dataset.symbol);
});

$("export").addEventListener("click", async () => {
  try {
    const response = await request("/api/v1/journal/executions.csv");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "botdca-recent-executions.csv";
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showNotice(
      response.headers.get("X-Export-Truncated") === "true"
        ? "Export contains only the latest 10,000 fills. Back up the database for complete history."
        : "Recent executions exported. Funding and transfers are not included.",
    );
  } catch (exportError) {
    showError(exportError.message);
  }
});

document.querySelectorAll("[data-route]").forEach((link) => {
  link.addEventListener("click", (event) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) {
      return;
    }
    event.preventDefault();
    navigate(link.dataset.route);
  });
});

window.addEventListener("popstate", () => {
  showPage(pageFromPath(window.location.pathname), { focus: true });
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load();
  else {
    lastSuccess = 0;
    updateControls();
  }
});

async function start() {
  showPage(pageFromPath(window.location.pathname));
  try {
    const session = await (
      await fetch("/api/v1/session", { cache: "no-store" })
    ).json();
    if (session.authentication_required) {
      setSessionConnected(false);
      setStatus("mode", "Console locked");
      showNotice(
        "Enter the operator token in Configuration to load account data and controls.",
      );
    } else {
      setSessionConnected(true, false);
      await load();
    }
  } catch {
    showError("Cannot reach the server. Refresh the page to reconnect.");
  }
  setInterval(() => {
    if (operatorToken || $("connect-form").hidden) load();
    updateControls();
  }, 5000);
}

start();
