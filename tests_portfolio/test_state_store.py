"""Tests for portfolio.state.store — round-trip schema + helpers against :memory: SQLite."""

from __future__ import annotations

import json
from datetime import date

import pytest

from portfolio.state import store


@pytest.fixture
def conn():
    c = store.connect(":memory:")
    store.init_schema(c)
    yield c
    c.close()


def test_init_schema_creates_tables(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {r["name"] for r in rows}
    assert {"agent_decisions", "trades", "positions_snapshot", "nav_history"} <= names


def test_log_decision_round_trip(conn):
    decision_id = store.log_decision(
        conn,
        ticker="NVDA",
        trade_date=date(2026, 6, 1),
        action="buy",
        reasoning="strong fundamentals",
        stop_loss=76.0,
        position_sizing="4-5% NAV",
        raw_state={"trader_msg": "test"},
    )
    row = conn.execute(
        "SELECT * FROM agent_decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    assert row["ticker"] == "NVDA"
    assert row["trade_date"] == "2026-06-01"
    assert row["action"] == "BUY"
    assert row["stop_loss"] == 76.0
    assert json.loads(row["raw_state"]) == {"trader_msg": "test"}


def test_log_trade_links_to_decision(conn):
    decision_id = store.log_decision(
        conn, ticker="AAPL", trade_date="2026-06-01", action="BUY"
    )
    trade_id = store.log_trade(
        conn,
        decision_id=decision_id,
        ticker="AAPL",
        side="buy",
        qty=10,
        alpaca_order_id="alpaca-abc-123",
    )
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["decision_id"] == decision_id
    assert row["status"] == "submitted"
    assert row["side"] == "buy"


def test_update_trade_status_to_filled(conn):
    decision_id = store.log_decision(
        conn, ticker="MSFT", trade_date="2026-06-01", action="BUY"
    )
    trade_id = store.log_trade(
        conn, decision_id=decision_id, ticker="MSFT", side="buy", qty=5
    )
    store.update_trade_status(
        conn,
        trade_id=trade_id,
        status="filled",
        filled_qty=5,
        filled_avg_price=420.50,
    )
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["status"] == "filled"
    assert row["filled_qty"] == 5
    assert row["filled_avg_price"] == 420.50


def test_snapshot_positions_replaces_same_day(conn):
    store.snapshot_positions(
        conn,
        snapshot_date="2026-06-01",
        positions=[
            {"ticker": "NVDA", "qty": 10, "avg_entry_price": 100.0, "market_value": 1050.0, "unrealized_pl": 50.0},
            {"ticker": "AAPL", "qty": 5, "avg_entry_price": 200.0},
        ],
    )
    # Re-snapshot same day with different data — should replace.
    store.snapshot_positions(
        conn,
        snapshot_date="2026-06-01",
        positions=[{"ticker": "NVDA", "qty": 12, "avg_entry_price": 105.0}],
    )
    rows = conn.execute(
        "SELECT ticker, qty FROM positions_snapshot WHERE snapshot_date = ? ORDER BY ticker",
        ("2026-06-01",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "NVDA"
    assert rows[0]["qty"] == 12


def test_log_nav_upsert(conn):
    store.log_nav(
        conn,
        snapshot_date="2026-06-01",
        portfolio_value=100_000,
        cash=100_000,
        equity=0,
    )
    # Upsert: same day, add SPY close
    store.log_nav(
        conn,
        snapshot_date="2026-06-01",
        portfolio_value=100_500,
        cash=50_000,
        equity=50_500,
        spy_close=580.42,
    )
    row = conn.execute(
        "SELECT * FROM nav_history WHERE snapshot_date = ?", ("2026-06-01",)
    ).fetchone()
    assert row["portfolio_value"] == 100_500
    assert row["spy_close"] == 580.42
