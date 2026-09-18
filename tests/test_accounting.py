import pytest

from botdca.accounting import build_accounting, classify_order_link_id


def _execution(link, *, symbol="HYPEUSDT", side="Buy", fee=0.01, pnl=0.0):
    return {
        "symbol": symbol,
        "side": side,
        "order_link_id": link,
        "fee": fee,
        "realized_pnl": pnl,
    }


def test_order_roles_are_derived_from_deterministic_bot_identities() -> None:
    assert classify_order_link_id("botdca-open-abc") == "entry"
    assert classify_order_link_id("botdca-dca-abc") == "dca"
    assert classify_order_link_id("botdca-tp-abc") == "close"
    assert classify_order_link_id("botdca-close-abc") == "close"
    assert classify_order_link_id("manual-order") == "unattributed"
    assert classify_order_link_id("") == "unattributed"


def test_fees_are_separated_into_entry_dca_and_close_buckets() -> None:
    accounting = build_accounting(
        executions=[
            _execution("botdca-open-1", fee=0.01),
            _execution("botdca-dca-1", fee=0.02),
            _execution("botdca-dca-2", fee=0.03),
            _execution("botdca-tp-1", side="Sell", fee=0.04, pnl=1.20),
        ]
    )
    hype = accounting.symbols["HYPEUSDT"]
    assert hype.entry_fees_usdt == pytest.approx(0.01)
    assert hype.dca_fees_usdt == pytest.approx(0.05)
    assert hype.close_fees_usdt == pytest.approx(0.04)
    assert hype.total_fees_usdt == pytest.approx(0.10)
    assert hype.realized_pnl_usdt == pytest.approx(1.20)
    assert hype.net_pnl_usdt == pytest.approx(1.10)


def test_a_manually_placed_fill_is_never_counted_as_strategy_pnl() -> None:
    accounting = build_accounting(
        executions=[
            _execution("botdca-tp-1", side="Sell", fee=0.04, pnl=1.00),
            _execution("manual-1", side="Sell", fee=0.10, pnl=50.0),
        ]
    )
    hype = accounting.symbols["HYPEUSDT"]
    assert hype.realized_pnl_usdt == pytest.approx(1.00)
    assert hype.unattributed_adjustments_usdt == pytest.approx(49.90)
    assert hype.unattributed_executions == 1
    # The windfall never leaks into net strategy P&L.
    assert hype.net_pnl_usdt == pytest.approx(0.96)


def test_negative_fees_are_recorded_as_rebates() -> None:
    accounting = build_accounting(executions=[_execution("botdca-dca-1", fee=-0.005)])
    hype = accounting.symbols["HYPEUSDT"]
    assert hype.rebates_usdt == pytest.approx(0.005)
    assert hype.dca_fees_usdt == pytest.approx(0.0)


def test_funding_is_attributed_per_symbol_from_the_transaction_log() -> None:
    accounting = build_accounting(
        executions=[_execution("botdca-open-1", fee=0.01)],
        transaction_log=[
            {"symbol": "HYPEUSDT", "type": "SETTLEMENT", "funding": -0.012},
            {"symbol": "ONDOUSDT", "type": "SETTLEMENT", "funding": 0.004},
        ],
        tracked_symbols=["HYPEUSDT", "ONDOUSDT"],
    )
    assert accounting.funding_available is True
    assert accounting.symbols["HYPEUSDT"].funding_usdt == pytest.approx(-0.012)
    assert accounting.symbols["ONDOUSDT"].funding_usdt == pytest.approx(0.004)


def test_deposits_and_transfers_never_become_strategy_pnl() -> None:
    accounting = build_accounting(
        executions=[_execution("botdca-tp-1", side="Sell", fee=0.01, pnl=2.0)],
        transaction_log=[
            {"symbol": "", "type": "TRANSFER_IN", "cashFlow": 500.0},
            {"symbol": "", "type": "WITHDRAW", "cashFlow": -100.0},
        ],
        tracked_symbols=["HYPEUSDT"],
    )
    # Strategy P&L is built from fills, never from the 400 USDT balance swing.
    assert accounting.symbols["HYPEUSDT"].net_pnl_usdt == pytest.approx(1.99)
    assert accounting.symbols["UNKNOWN"].unattributed_adjustments_usdt == pytest.approx(400.0)


def test_trade_rows_in_the_transaction_log_are_not_double_counted() -> None:
    accounting = build_accounting(
        executions=[_execution("botdca-tp-1", side="Sell", fee=0.0, pnl=3.0)],
        transaction_log=[{"symbol": "HYPEUSDT", "type": "TRADE", "cashFlow": 3.0}],
        tracked_symbols=["HYPEUSDT"],
    )
    assert accounting.symbols["HYPEUSDT"].realized_pnl_usdt == pytest.approx(3.0)


def test_portfolio_totals_sum_every_tracked_symbol() -> None:
    accounting = build_accounting(
        executions=[
            _execution("botdca-tp-1", symbol="HYPEUSDT", side="Sell", fee=0.01, pnl=1.0),
            _execution("botdca-tp-2", symbol="ONDOUSDT", side="Sell", fee=0.02, pnl=2.0),
        ]
    )
    totals = accounting.totals
    assert totals.realized_pnl_usdt == pytest.approx(3.0)
    assert totals.close_fees_usdt == pytest.approx(0.03)
    assert totals.net_pnl_usdt == pytest.approx(2.97)


def test_limitations_are_always_stated_and_truncation_is_never_silent() -> None:
    described = build_accounting(executions=[], window_truncated=True).describe()
    assert described["window_truncated"] is True
    assert any("truncated" in note for note in described["limitations"])
    assert any("wallet-balance delta" in note for note in described["limitations"])

    unqueried = build_accounting(executions=[]).describe()
    assert unqueried["funding_available"] is False
    assert any("Funding was not queried" in note for note in unqueried["limitations"])
