"""FastAPI dashboard server — bento grid + clickable decision deep-dive.

Run locally:
    .venv/bin/uvicorn portfolio.dashboard.server:app --reload --port 8080

Production:
    uvicorn portfolio.dashboard.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import json
import os
import re
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


_SIGNAL_PATTERNS = [
    (re.compile(r"FINAL TRANSACTION PROPOSAL.*?(BUY|SELL|HOLD)", re.I | re.S), 0),
    (re.compile(r"\*?\*?(Rating|Recommendation|Action)\*?\*?\s*[:：]\s*\*?\*?\s*(Buy|Overweight|Hold|Underweight|Sell)", re.I), 1),
]

_SIGNAL_TO_TONE = {
    "buy": ("Bullish", "bull"),
    "overweight": ("Bullish", "bull"),
    "hold": ("Neutral", "neutral"),
    "underweight": ("Bearish", "bear"),
    "sell": ("Bearish", "bear"),
    "bullish": ("Bullish", "bull"),
    "bearish": ("Bearish", "bear"),
}

_METRIC_PATTERNS = [
    # Order matters — most specific first.
    re.compile(r"RSI[:\s]+(\d+\.?\d*)", re.I),
    re.compile(r"P\/E[:\s\(TTM\)]*[:\s]+(\d+\.?\d*)", re.I),
    re.compile(r"forward\s*P\/E[:\s]+(\d+\.?\d*)", re.I),
    re.compile(r"FCF[:\s\(TTM\)]*[:\s]+\$?(\d+\.?\d*[KMB]?)", re.I),
    re.compile(r"Free\s*Cash\s*Flow[:\s]+\$?(\d+\.?\d*[KMB]?)", re.I),
    re.compile(r"price\s*target[:\s]+\$?(\d+\.?\d*)", re.I),
]


def _strip_md(s: str) -> str:
    """Strip simple markdown bold/italic markers."""
    s = re.sub(r"\*{1,3}([^*\n]+?)\*{1,3}", r"\1", s)
    s = re.sub(r"`([^`]+?)`", r"\1", s)
    return s.strip()


def _extract_take(text: str | None, max_chars: int = 220) -> str:
    """Find the most decisive sentence — the agent's actual call, not just the lead.

    Tries explicit verdict markers first (Bottom line, Conclusion, Final
    transaction proposal, Action, Recommendation, etc.), then falls back to
    a short first informative line.
    """
    if not text:
        return ""
    # Look for an explicit verdict line, in priority order.
    patterns = [
        r"(?:^|\n)\s*\*{0,2}(?:Bottom line|Bottom-line|Conclusion|Final verdict|The bottom line)\*{0,2}\s*[:：]?\s*\*{0,2}(.+?)(?=\n\n|\n\s*\*\*|\Z)",
        r"FINAL TRANSACTION PROPOSAL\s*[:：]\s*\*{0,2}\s*(.+?)(?=\n|\Z)",
        r"\*{0,2}(?:Recommendation|Action|Trading Implication)\*{0,2}\s*[:：]\s*\*{0,2}\s*(.+?)(?=\n\n|\n\s*\*\*|\Z)",
        r"\*{0,2}Practical (?:trading )?view\*{0,2}\s*[:：]?\s*(.+?)(?=\n\n|\n\s*\*\*|\Z)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I | re.S)
        if m:
            line = _strip_md(m.group(1).strip())
            # Trim to first sentence if multi-sentence.
            parts = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)
            line = parts[0] if parts else line
            line = line.strip().rstrip(".") + "."
            if len(line) > max_chars:
                line = line[: max_chars - 1].rstrip() + "…"
            if 12 < len(line):
                return line

    # Fallback: first informative non-header sentence.
    cleaned = re.sub(r"^FINAL TRANSACTION PROPOSAL.*?\n+", "", text, flags=re.I | re.S)
    lines = [l for l in cleaned.split("\n") if l.strip() and not l.strip().startswith(("#", "-", "*", "|"))]
    if not lines:
        return ""
    first = _strip_md(lines[0])
    parts = re.split(r"(?<=[.!?])\s+", first, maxsplit=1)
    out = parts[0] if parts else first
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out


# Backwards-compat alias used elsewhere in this file.
_summarize_agent = _extract_take


def _signal_from_text(text: str | None) -> dict:
    """Return {label: 'Bullish'|'Neutral'|'Bearish', tone: css_class} or empty dict."""
    if not text:
        return {}
    for pat, grp in _SIGNAL_PATTERNS:
        m = pat.search(text)
        if m:
            verdict = m.group(grp + 1).lower() if grp == 1 else m.group(1).lower()
            if verdict in _SIGNAL_TO_TONE:
                label, tone = _SIGNAL_TO_TONE[verdict]
                return {"label": label, "tone": tone, "raw": verdict.title()}
    # Fallback: scan for first stance word that appears (broader vocabulary).
    for word in ("Buy", "Overweight", "Hold", "Underweight", "Sell", "Bullish", "Bearish", "Neutral"):
        if re.search(rf"\b{word}\b", text):
            label, tone = _SIGNAL_TO_TONE[word.lower()]
            return {"label": label, "tone": tone, "raw": word}
    return {}


def _extract_metrics(text: str | None) -> list[str]:
    """Pull out numeric chips like 'RSI 78.63' / 'P/E 37' from agent text."""
    if not text:
        return []
    out: list[str] = []
    rsi = re.search(r"RSI[:\s]+(\d+\.?\d*)", text, re.I)
    if rsi:
        out.append(f"RSI {rsi.group(1)}")
    pe = re.search(r"\bP/E(?:\s*\(TTM\))?[:\s]+(\d+\.?\d*)", text, re.I)
    if pe:
        out.append(f"P/E {pe.group(1)}")
    fcf = re.search(r"FCF.{0,20}?\$?(\d+\.?\d*\s*[KMB]?)", text, re.I)
    if fcf:
        val = fcf.group(1).strip()
        out.append(f"FCF ${val}")
    pt = re.search(r"price\s*target.{0,20}?\$?(\d+\.?\d*)", text, re.I)
    if pt:
        out.append(f"PT ${pt.group(1)}")
    close = re.search(r"closed\s*at\s*\*?\*?(\d+\.?\d*)", text, re.I)
    if close:
        out.append(f"Last ${close.group(1)}")
    return out[:4]  # cap at 4 chips


def _build_agent_views(state: dict) -> list[dict]:
    """Per-agent: punchline + signal chip + key metric chips + full text."""
    agents = [
        ("market_report", "Market Analyst", "📈"),
        ("sentiment_report", "Sentiment Analyst", "💬"),
        ("news_report", "News Analyst", "📰"),
        ("fundamentals_report", "Fundamentals Analyst", "📊"),
        ("investment_plan", "Research Manager", "⚖️"),
        ("trader_investment_plan", "Trader", "🎯"),
    ]
    views = []
    for key, label, emoji in agents:
        text = state.get(key) or ""
        if not text:
            continue
        views.append({
            "key": key,
            "label": label,
            "emoji": emoji,
            "summary": _summarize_agent(text),
            "signal": _signal_from_text(text),
            "metrics": _extract_metrics(text),
            "full": text,
        })
    return views


def _pm_summary(state: dict) -> dict:
    """Extract PM final verdict's key elements: rating, summary, price target, time horizon, stop."""
    text = state.get("final_trade_decision") or ""
    if not text:
        return {}
    rating_match = re.search(r"\*?\*?Rating\*?\*?\s*[:：]\s*\*?\*?\s*(Buy|Overweight|Hold|Underweight|Sell)", text, re.I)
    summary_match = re.search(r"\*?\*?Executive Summary\*?\*?\s*[:：]\s*(.+?)(?=\n\s*\*\*|\Z)", text, re.S | re.I)
    pt_match = re.search(r"\*?\*?Price Target\*?\*?\s*[:：]\s*\$?(\d+\.?\d*)", text, re.I)
    horizon_match = re.search(r"\*?\*?Time Horizon\*?\*?\s*[:：]\s*([^\n]+)", text, re.I)
    return {
        "rating": rating_match.group(1).title() if rating_match else None,
        "summary": (summary_match.group(1).strip() if summary_match else "")[:400],
        "price_target": pt_match.group(1) if pt_match else None,
        "horizon": horizon_match.group(1).strip() if horizon_match else None,
    }


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
        state = json.loads(out.pop("raw_state") or "{}")
    except Exception:
        state = {}
    out["state"] = state
    out["agents"] = _build_agent_views(state)
    out["pm"] = _pm_summary(state)
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
