"""Strategy accounting.

Strategy profit and loss is built from attributable trading cash flows, never
from a wallet-balance delta. Deposits, withdrawals, internal transfers and
unrelated positions all move the account balance without being strategy
results, so anything that cannot be attributed to a bot order is preserved in
an explicit `unattributed_adjustments` bucket rather than silently absorbed.

Limitations, stated plainly:

- Fees are separated into entry, DCA and close only as far as the bot's own
  deterministic order identities allow. A fill on a manually placed order is
  never attributed to the strategy.
- Funding is charged per symbol at the account level, not per basket. When
  several baskets on the same symbol span a funding timestamp, funding is
  attributed to the symbol, not split across baskets.
- Rebates appear as negative fees where Bybit reports them that way.
- Bybit's transaction log is paginated and time-bounded. Anything outside the
  queried window is reported as `window_truncated`, not assumed to be zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

ENTRY_PREFIX = "botdca-open-"
DCA_PREFIX = "botdca-dca-"
TP_PREFIX = "botdca-tp-"
CLOSE_PREFIX = "botdca-close-"
BOT_PREFIX = "botdca-"


def classify_order_link_id(order_link_id: str) -> str:
    """Map a deterministic bot order identity onto an accounting role."""
    link = order_link_id or ""
    if link.startswith(ENTRY_PREFIX):
        return "entry"
    if link.startswith(DCA_PREFIX):
        return "dca"
    if link.startswith((TP_PREFIX, CLOSE_PREFIX)):
        return "close"
    if link.startswith(BOT_PREFIX):
        return "other_bot"
    return "unattributed"


@dataclass(slots=True)
class SymbolAccounting:
    """Attributed trading cash flows for one symbol."""

    symbol: str
    realized_pnl_usdt: float = 0.0
    entry_fees_usdt: float = 0.0
    dca_fees_usdt: float = 0.0
    close_fees_usdt: float = 0.0
    other_bot_fees_usdt: float = 0.0
    funding_usdt: float = 0.0
    rebates_usdt: float = 0.0
    unattributed_adjustments_usdt: float = 0.0
    buy_executions: int = 0
    sell_executions: int = 0
    unattributed_executions: int = 0

    @property
    def total_fees_usdt(self) -> float:
        return (
            self.entry_fees_usdt
            + self.dca_fees_usdt
            + self.close_fees_usdt
            + self.other_bot_fees_usdt
        )

    @property
    def net_pnl_usdt(self) -> float:
        """Realized P&L after fees, funding and rebates. Excludes the unattributed bucket."""
        return self.realized_pnl_usdt - self.total_fees_usdt + self.funding_usdt + self.rebates_usdt

    def describe(self) -> dict:
        return {
            "symbol": self.symbol,
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "entry_fees_usdt": self.entry_fees_usdt,
            "dca_fees_usdt": self.dca_fees_usdt,
            "close_fees_usdt": self.close_fees_usdt,
            "other_bot_fees_usdt": self.other_bot_fees_usdt,
            "total_fees_usdt": self.total_fees_usdt,
            "funding_usdt": self.funding_usdt,
            "rebates_usdt": self.rebates_usdt,
            "net_pnl_usdt": self.net_pnl_usdt,
            "unattributed_adjustments_usdt": self.unattributed_adjustments_usdt,
            "buy_executions": self.buy_executions,
            "sell_executions": self.sell_executions,
            "unattributed_executions": self.unattributed_executions,
        }


@dataclass(slots=True)
class PortfolioAccounting:
    symbols: dict[str, SymbolAccounting] = field(default_factory=dict)
    window_truncated: bool = False
    funding_available: bool = False
    notes: list[str] = field(default_factory=list)

    def for_symbol(self, symbol: str) -> SymbolAccounting:
        key = symbol.upper()
        if key not in self.symbols:
            self.symbols[key] = SymbolAccounting(symbol=key)
        return self.symbols[key]

    @property
    def totals(self) -> SymbolAccounting:
        total = SymbolAccounting(symbol="PORTFOLIO")
        for entry in self.symbols.values():
            total.realized_pnl_usdt += entry.realized_pnl_usdt
            total.entry_fees_usdt += entry.entry_fees_usdt
            total.dca_fees_usdt += entry.dca_fees_usdt
            total.close_fees_usdt += entry.close_fees_usdt
            total.other_bot_fees_usdt += entry.other_bot_fees_usdt
            total.funding_usdt += entry.funding_usdt
            total.rebates_usdt += entry.rebates_usdt
            total.unattributed_adjustments_usdt += entry.unattributed_adjustments_usdt
            total.buy_executions += entry.buy_executions
            total.sell_executions += entry.sell_executions
            total.unattributed_executions += entry.unattributed_executions
        return total

    def describe(self) -> dict:
        return {
            "per_symbol": {k: v.describe() for k, v in sorted(self.symbols.items())},
            "portfolio": self.totals.describe(),
            "funding_available": self.funding_available,
            "window_truncated": self.window_truncated,
            "limitations": [
                (
                    "Strategy P&L is attributed from bot order identities, never from a "
                    "wallet-balance delta."
                ),
                "Funding is attributed per symbol, not per basket.",
                (
                    "Cash flows that cannot be attributed to a bot order are preserved in "
                    "unattributed_adjustments_usdt."
                ),
                *self.notes,
            ],
        }


def accumulate_executions(
    accounting: PortfolioAccounting,
    executions: Iterable[dict[str, Any]],
) -> PortfolioAccounting:
    """Fold persisted execution rows into per-symbol attributed buckets."""
    for row in executions:
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        entry = accounting.for_symbol(symbol)
        fee = float(row.get("fee") or 0.0)
        realized = float(row.get("realized_pnl") or 0.0)
        role = classify_order_link_id(str(row.get("order_link_id") or ""))
        side = str(row.get("side") or "")

        if role == "unattributed":
            entry.unattributed_adjustments_usdt += realized - fee
            entry.unattributed_executions += 1
            continue

        if side == "Buy":
            entry.buy_executions += 1
        elif side == "Sell":
            entry.sell_executions += 1

        entry.realized_pnl_usdt += realized
        # Bybit reports a maker rebate as a negative fee.
        if fee < 0:
            entry.rebates_usdt += -fee
        elif role == "entry":
            entry.entry_fees_usdt += fee
        elif role == "dca":
            entry.dca_fees_usdt += fee
        elif role == "close":
            entry.close_fees_usdt += fee
        else:
            entry.other_bot_fees_usdt += fee
    return accounting


#: Bybit transaction-log types that are strategy funding rather than trades.
FUNDING_TYPES = {"SETTLEMENT", "FUNDING"}
#: Types that move the wallet without being a strategy result.
NON_STRATEGY_TYPES = {"TRANSFER_IN", "TRANSFER_OUT", "DEPOSIT", "WITHDRAW", "AIRDROP"}


def accumulate_transaction_log(
    accounting: PortfolioAccounting,
    rows: Sequence[dict[str, Any]],
    *,
    tracked_symbols: Iterable[str] = (),
) -> PortfolioAccounting:
    """Fold a Bybit transaction log into funding and non-strategy buckets.

    Trade rows are ignored here: they are already attributed from persisted
    executions, and counting both would double-count realized P&L.
    """
    tracked = {symbol.upper() for symbol in tracked_symbols}
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        kind = str(row.get("type") or "").upper()
        cash_flow = float(row.get("cashFlow") or 0.0)
        funding = float(row.get("funding") or 0.0)

        if kind in FUNDING_TYPES:
            if tracked and symbol not in tracked:
                continue
            accounting.funding_available = True
            accounting.for_symbol(symbol or "UNKNOWN").funding_usdt += funding or cash_flow
            continue
        if kind in NON_STRATEGY_TYPES:
            # Deliberately not folded into strategy P&L; recorded so the operator
            # can see why wallet balance and strategy P&L disagree.
            accounting.for_symbol(
                symbol or "UNKNOWN"
            ).unattributed_adjustments_usdt += cash_flow
    return accounting


def build_accounting(
    *,
    executions: Iterable[dict[str, Any]],
    transaction_log: Sequence[dict[str, Any]] | None = None,
    tracked_symbols: Iterable[str] = (),
    window_truncated: bool = False,
) -> PortfolioAccounting:
    accounting = PortfolioAccounting(window_truncated=window_truncated)
    for symbol in tracked_symbols:
        accounting.for_symbol(symbol)
    accumulate_executions(accounting, executions)
    if transaction_log is None:
        accounting.notes.append(
            "Funding was not queried; funding_usdt is zero rather than unknown."
        )
    else:
        accumulate_transaction_log(
            accounting, transaction_log, tracked_symbols=tracked_symbols
        )
    if window_truncated:
        accounting.notes.append(
            "The queried transaction window was truncated; totals are incomplete."
        )
    return accounting
