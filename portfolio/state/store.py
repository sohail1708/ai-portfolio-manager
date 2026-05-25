"""SQLite-backed state store for trades, decisions, positions, and NAV."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

DEFAULT_DB_PATH = "portfolio_state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    action TEXT NOT NULL,
    reasoning TEXT,
    stop_loss REAL,
    position_sizing TEXT,
    raw_state TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    decision_id INTEGER REFERENCES agent_decisions(id),
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    order_type TEXT NOT NULL,
    limit_price REAL,
    status TEXT NOT NULL,
    filled_qty REAL DEFAULT 0,
    filled_avg_price REAL,
    alpaca_order_id TEXT UNIQUE,
    error TEXT
);

CREATE TABLE IF NOT EXISTS positions_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_date TEXT NOT NULL,
    ticker TEXT NOT NULL,
    qty REAL NOT NULL,
    avg_entry_price REAL NOT NULL,
    market_value REAL,
    unrealized_pl REAL,
    UNIQUE(snapshot_date, ticker)
);

CREATE TABLE IF NOT EXISTS nav_history (
    snapshot_date TEXT PRIMARY KEY,
    portfolio_value REAL NOT NULL,
    cash REAL NOT NULL,
    equity REAL NOT NULL,
    spy_close REAL,
    qqq_close REAL
);

CREATE INDEX IF NOT EXISTS idx_decisions_ticker_date
    ON agent_decisions(ticker, trade_date);
CREATE INDEX IF NOT EXISTS idx_trades_decision
    ON trades(decision_id);
CREATE INDEX IF NOT EXISTS idx_positions_date
    ON positions_snapshot(snapshot_date);
"""


def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Idempotent column adds for DBs created before the column existed.
    _try_add_column(conn, "nav_history", "qqq_close", "REAL")
    conn.commit()


def _try_add_column(
    conn: sqlite3.Connection, table: str, column: str, col_type: str
) -> None:
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            raise


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def log_decision(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    trade_date: str | date,
    action: str,
    reasoning: str | None = None,
    stop_loss: float | None = None,
    position_sizing: str | None = None,
    raw_state: dict[str, Any] | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO agent_decisions
            (ticker, trade_date, action, reasoning, stop_loss, position_sizing, raw_state)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticker,
            _iso(trade_date),
            action.upper(),
            reasoning,
            stop_loss,
            position_sizing,
            json.dumps(raw_state, default=str) if raw_state is not None else None,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def log_trade(
    conn: sqlite3.Connection,
    *,
    decision_id: int | None,
    ticker: str,
    side: str,
    qty: float,
    order_type: str = "market",
    limit_price: float | None = None,
    status: str = "submitted",
    alpaca_order_id: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO trades
            (decision_id, ticker, side, qty, order_type, limit_price, status, alpaca_order_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (decision_id, ticker, side.lower(), qty, order_type, limit_price, status, alpaca_order_id),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def update_trade_status(
    conn: sqlite3.Connection,
    *,
    trade_id: int,
    status: str,
    filled_qty: float | None = None,
    filled_avg_price: float | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE trades
        SET status = ?,
            filled_qty = COALESCE(?, filled_qty),
            filled_avg_price = COALESCE(?, filled_avg_price),
            error = COALESCE(?, error)
        WHERE id = ?
        """,
        (status, filled_qty, filled_avg_price, error, trade_id),
    )
    conn.commit()


def snapshot_positions(
    conn: sqlite3.Connection,
    *,
    snapshot_date: str | date,
    positions: Sequence[dict[str, Any]],
) -> None:
    iso_date = _iso(snapshot_date)
    rows = [
        (
            iso_date,
            p["ticker"],
            p["qty"],
            p["avg_entry_price"],
            p.get("market_value"),
            p.get("unrealized_pl"),
        )
        for p in positions
    ]
    with transaction(conn):
        conn.execute(
            "DELETE FROM positions_snapshot WHERE snapshot_date = ?", (iso_date,)
        )
        conn.executemany(
            """
            INSERT INTO positions_snapshot
                (snapshot_date, ticker, qty, avg_entry_price, market_value, unrealized_pl)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def log_nav(
    conn: sqlite3.Connection,
    *,
    snapshot_date: str | date,
    portfolio_value: float,
    cash: float,
    equity: float,
    spy_close: float | None = None,
    qqq_close: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO nav_history (snapshot_date, portfolio_value, cash, equity, spy_close, qqq_close)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_date) DO UPDATE SET
            portfolio_value = excluded.portfolio_value,
            cash = excluded.cash,
            equity = excluded.equity,
            spy_close = COALESCE(excluded.spy_close, nav_history.spy_close),
            qqq_close = COALESCE(excluded.qqq_close, nav_history.qqq_close)
        """,
        (_iso(snapshot_date), portfolio_value, cash, equity, spy_close, qqq_close),
    )
    conn.commit()


def _iso(d: str | date) -> str:
    if isinstance(d, date) and not isinstance(d, datetime):
        return d.isoformat()
    if isinstance(d, datetime):
        return d.date().isoformat()
    return d
