"""FastAPI dashboard server — bento grid + clickable decision deep-dive.

Run locally:
    .venv/bin/uvicorn portfolio.dashboard.server:app --reload --port 8080

Production:
    uvicorn portfolio.dashboard.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from portfolio.state import store

load_dotenv()

BASE_DIR = Path(__file__).parent
_jinja = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=select_autoescape(["html"]),
)

LAUNCH_DATE = date(2026, 6, 1)
END_DATE = date(2026, 6, 30)
TOTAL_DAYS = 30

app = FastAPI(title="AI Portfolio Manager — vs QQQ")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def _db_path() -> str:
    return os.environ.get("PORTFOLIO_DB_PATH", "./portfolio_state.db")


def _starting_cash() -> float:
    return float(os.environ.get("PORTFOLIO_STARTING_CASH", "100000"))


def _trading_days_between(start_exclusive: date, end_exclusive: date) -> int:
    days = 0
    d = start_exclusive + timedelta(days=1)
    while d < end_exclusive:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def _day_label() -> dict:
    today = date.today()
    if today < LAUNCH_DATE:
        n = _trading_days_between(today, LAUNCH_DATE)
        return {
            "label": f"T-{n}",
            "phase": "Test phase",
            "sub": f"{n} trading day{'s' if n != 1 else ''} until June 1 launch",
            "is_live": False,
        }
    if today > END_DATE:
        return {"label": "Final", "phase": "Complete", "sub": "Run complete", "is_live": False}
    day_num = (today - LAUNCH_DATE).days + 1
    return {
        "label": f"Day {day_num}",
        "phase": "Live",
        "sub": f"{day_num} of {TOTAL_DAYS}",
        "is_live": True,
    }


def _load_nav(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT snapshot_date, portfolio_value, cash, equity, spy_close, qqq_close "
        "FROM nav_history ORDER BY snapshot_date"
    ).fetchall()
    return [dict(r) for r in rows]


def _compute_benchmarks(nav: list[dict], starting_cash: float) -> list[dict]:
    if not nav:
        return []
    out = []
    first_qqq = next((r["qqq_close"] for r in nav if r["qqq_close"]), None)
    first_spy = next((r["spy_close"] for r in nav if r["spy_close"]), None)
    for r in nav:
        row = dict(r)
        row["qqq_eq"] = (
            starting_cash * (r["qqq_close"] / first_qqq) if first_qqq and r["qqq_close"] else None
        )
        row["spy_eq"] = (
            starting_cash * (r["spy_close"] / first_spy) if first_spy and r["spy_close"] else None
        )
        out.append(row)
    return out


def _compute_streak(nav: list[dict]) -> dict:
    if len(nav) < 2:
        return {"days_won": 0, "days_total": max(0, len(nav) - 1), "best": None, "worst": None}
    days_won = 0
    days_total = 0
    best, worst = None, None
    for i in range(1, len(nav)):
        prev, curr = nav[i - 1], nav[i]
        p_ret = (curr["portfolio_value"] - prev["portfolio_value"]) / prev["portfolio_value"]
        if curr.get("qqq_close") and prev.get("qqq_close"):
            q_ret = (curr["qqq_close"] - prev["qqq_close"]) / prev["qqq_close"]
            alpha_today = p_ret - q_ret
            days_total += 1
            if alpha_today > 0:
                days_won += 1
            if best is None or alpha_today > best["alpha"]:
                best = {"date": curr["snapshot_date"], "alpha": alpha_today}
            if worst is None or alpha_today < worst["alpha"]:
                worst = {"date": curr["snapshot_date"], "alpha": alpha_today}
    return {"days_won": days_won, "days_total": days_total, "best": best, "worst": worst}


def _latest_decisions(conn: sqlite3.Connection) -> list[dict]:
    """Today's decisions if any, otherwise the most recent trade-date's batch."""
    row = conn.execute(
        "SELECT MAX(trade_date) FROM agent_decisions"
    ).fetchone()
    if not row or not row[0]:
        return []
    latest_date = row[0]
    rows = conn.execute(
        "SELECT id, ticker, trade_date, action, reasoning, raw_state "
        "FROM agent_decisions WHERE trade_date = ? ORDER BY id",
        (latest_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def _decision_full(conn: sqlite3.Connection, decision_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, ticker, trade_date, action, reasoning, raw_state "
        "FROM agent_decisions WHERE id = ?",
        (decision_id,),
    ).fetchone()
    if not row:
        return None
    out = dict(row)
    try:
        out["state"] = json.loads(out.pop("raw_state") or "{}")
    except Exception:
        out["state"] = {}
    return out


def _positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM positions_snapshot "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM positions_snapshot) "
        "ORDER BY market_value DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _build_view_model() -> dict:
    starting_cash = _starting_cash()
    db_path = _db_path()
    if not os.path.exists(db_path):
        return {
            "day": _day_label(),
            "has_data": False,
            "hero": {
                "portfolio_value": starting_cash,
                "qqq_eq": starting_cash,
                "alpha_pct": 0.0,
                "alpha_usd": 0.0,
                "winning": None,
            },
            "nav_series": [],
            "streak": {"days_won": 0, "days_total": 0, "best": None, "worst": None},
            "positions": [],
            "decisions": [],
            "decisions_json": "[]",
            "decisions_by_rating": {},
        }

    conn = store.connect(db_path)
    nav = _compute_benchmarks(_load_nav(conn), starting_cash)
    positions = _positions(conn)
    decisions = _latest_decisions(conn)

    # Attach full state to each decision for client-side modal
    decisions_full = []
    for d in decisions:
        full = _decision_full(conn, d["id"])
        if full:
            decisions_full.append(full)

    if nav:
        latest = nav[-1]
        portfolio_value = float(latest["portfolio_value"])
        qqq_eq = float(latest["qqq_eq"]) if latest.get("qqq_eq") else starting_cash
        alpha_pct = (portfolio_value - qqq_eq) / qqq_eq * 100 if qqq_eq else 0.0
        alpha_usd = portfolio_value - qqq_eq
        winning = alpha_pct > 0 if alpha_pct != 0 else None
    else:
        portfolio_value = starting_cash
        qqq_eq = starting_cash
        alpha_pct = 0.0
        alpha_usd = 0.0
        winning = None

    decisions_by_rating: dict[str, int] = {}
    for d in decisions:
        rating = (d["action"] or "Hold").title()
        decisions_by_rating[rating] = decisions_by_rating.get(rating, 0) + 1

    return {
        "day": _day_label(),
        "has_data": bool(nav) or bool(decisions),
        "hero": {
            "portfolio_value": portfolio_value,
            "qqq_eq": qqq_eq,
            "alpha_pct": alpha_pct,
            "alpha_usd": alpha_usd,
            "winning": winning,
        },
        "nav_series": [
            {
                "date": str(r["snapshot_date"]),
                "portfolio": float(r["portfolio_value"]),
                "qqq_eq": float(r["qqq_eq"]) if r.get("qqq_eq") else None,
                "spy_eq": float(r["spy_eq"]) if r.get("spy_eq") else None,
            }
            for r in nav
        ],
        "streak": _compute_streak(nav),
        "positions": positions,
        "decisions": decisions_full,
        "decisions_json": json.dumps(decisions_full, default=str),
        "decisions_by_rating": decisions_by_rating,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    vm = _build_view_model()
    html = _jinja.get_template("index.html").render(**vm)
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {"status": "ok", "ts": datetime.utcnow().isoformat()}


@app.get("/api/decisions/{decision_id}")
async def api_decision(decision_id: int):
    conn = store.connect(_db_path())
    d = _decision_full(conn, decision_id)
    if not d:
        return {"error": "not found"}
    return d
