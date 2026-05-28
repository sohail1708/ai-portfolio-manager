"""FastAPI dashboard server — bento grid + clickable decision deep-dive.

Run locally:
    .venv/bin/uvicorn portfolio.dashboard.server:app --reload --port 8080

Production:
    uvicorn portfolio.dashboard.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

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

LAUNCH_DATE = date(2026, 5, 29)
END_DATE = date(2026, 6, 27)
TOTAL_DAYS = 30
UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "ORCL"
]
CASH_PARK_TICKER = "QQQ"

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
        cal_days = (LAUNCH_DATE - today).days
        if cal_days == 1:
            label = "Eve"
            sub = "Day 1 starts tomorrow at 21:00 BST"
        else:
            label = f"T-{cal_days}"
            sub = f"{cal_days} day{'s' if cal_days != 1 else ''} until launch"
        return {
            "label": label,
            "phase": "Pre-launch",
            "sub": sub,
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
    "add": ("Bullish", "bull"),
    "hold": ("Neutral", "neutral"),
    "underweight": ("Bearish", "bear"),
    "trim": ("Bearish", "bear"),
    "sell": ("Bearish", "bear"),
    "bullish": ("Bullish", "bull"),
    "bearish": ("Bearish", "bear"),
    "neutral": ("Neutral", "neutral"),
}

# Display-only relabel: upstream gives "Overweight" / "Underweight" — we
# show action verbs because that's what they actually mean in our system.
_RATING_DISPLAY = {
    "buy": ("Buy", "buy"),
    "overweight": ("Add", "add"),
    "hold": ("Hold", "hold"),
    "underweight": ("Trim", "trim"),
    "sell": ("Sell", "sell"),
}


def display_rating(raw: str | None) -> tuple[str, str]:
    """Return (display_text, css_class_suffix) for a raw 5-tier rating."""
    if not raw:
        return ("—", "hold")
    key = raw.strip().lower()
    return _RATING_DISPLAY.get(key, (raw.title(), key))

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
_extract_verdict = _extract_take


def _extract_reasoning(text: str | None, max_chars: int = 240) -> str:
    """Pull a 1–2 sentence rationale — the *why* behind the agent's call.

    Different from _extract_verdict: that one finds the decisive call
    ("Buy" / "Bottom line: …"); this one finds an analytical sentence
    that *explains* the call (usually the first or second paragraph).
    """
    if not text:
        return ""
    # Drop "FINAL TRANSACTION PROPOSAL: X" preamble — it's the verdict not the reason.
    cleaned = re.sub(r"^FINAL TRANSACTION PROPOSAL.*?\n+", "", text, flags=re.I | re.S)
    # Skip header lines (#), list markers (-, *), table lines (|), and
    # bracket-marker lines ([2026-...]).
    lines = [
        l.strip()
        for l in cleaned.split("\n")
        if l.strip() and not l.strip().startswith(("#", "-", "*", "|", "["))
    ]
    if not lines:
        return ""
    # Walk the first few non-trivial lines; pick the first long enough one.
    candidate = ""
    for line in lines[:6]:
        stripped = _strip_md(line)
        if len(stripped) < 40:
            continue
        candidate = stripped
        break
    if not candidate:
        candidate = _strip_md(lines[0])
    sentences = re.split(r"(?<=[.!?])\s+", candidate)
    # Take up to 2 sentences for a fuller "why".
    out = " ".join(sentences[:2]) if len(sentences) > 1 else sentences[0]
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "…"
    return out


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


_AGENT_DEFS = [
    ("market_report", "Market Analyst", "📈", "an equity technical analyst"),
    ("sentiment_report", "Sentiment Analyst", "💬", "an equity sentiment analyst"),
    ("news_report", "News Analyst", "📰", "an equity news/macro analyst"),
    ("fundamentals_report", "Fundamentals Analyst", "📊", "an equity fundamentals analyst"),
    ("investment_plan", "Research Manager", "⚖️", "a research manager synthesizing bull/bear debate"),
    ("trader_investment_plan", "Trader", "🎯", "a sell-side trader writing a transaction proposal"),
]


_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "stance": {"type": "string", "enum": ["Bullish", "Bearish", "Neutral"]},
        "headline": {
            "type": "string",
            "description": "ONE sentence stating the agent's actual call/conclusion. Max 20 words. No hedging language like 'overall'.",
        },
        "reason": {
            "type": "string",
            "description": "ONE sentence explaining WHY they made that call. Max 25 words. Cite specific data the agent referenced.",
        },
        "key_points": {
            "type": "array",
            "items": {"type": "string"},
            "description": "2 to 3 short bullets (3-7 words each) of specific evidence: prices, ratios, catalysts, levels.",
        },
    },
    "required": ["stance", "headline", "reason", "key_points"],
    "additionalProperties": False,
}


_openai_client = None
_summarizer_model = os.environ.get("DASHBOARD_SUMMARIZER_MODEL", "gpt-5.4-mini")


def _get_openai():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    return _openai_client


def _summarize_with_llm(text: str, agent_role: str) -> dict | None:
    """Call LLM to produce a structured summary of a verbose agent report."""
    if not text or not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        client = _get_openai()
        prompt = (
            f"You are summarizing {agent_role}'s analysis of a stock for a one-glance dashboard card.\n\n"
            "Pull out:\n"
            "- stance: Bullish / Bearish / Neutral — the overall directional view\n"
            "- headline: ONE crisp sentence (≤20 words) stating their actual call/conclusion. Pull from 'Bottom line' / 'Recommendation' / 'Final view' if present. No throat-clearing.\n"
            "- reason: ONE crisp sentence (≤25 words) explaining WHY. Cite specific numbers/data the agent referenced.\n"
            "- key_points: 2–3 short evidence bullets (3–7 words each). E.g., 'RSI 78 — overbought', 'BofA target $380', 'P/E 37 expensive'.\n\n"
            "Be specific, not generic. Don't paraphrase the agent's hedging — surface the conclusion.\n\n"
            f"Agent's full report:\n{text[:5000]}"
        )
        resp = client.chat.completions.create(
            model=_summarizer_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "agent_summary", "strict": True, "schema": _SUMMARY_SCHEMA},
            },
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.warning("LLM summarization failed for %s: %s", agent_role, e)
        return None


def _summarize_decision_state(state: dict) -> dict:
    """Generate LLM summaries for all agent reports in a state dict (parallel)."""
    summaries: dict = {}
    tasks: list[tuple[str, str, str]] = []
    for key, label, _emoji, role in _AGENT_DEFS:
        text = state.get(key) or ""
        if text.strip():
            tasks.append((key, role, text))
    if not tasks:
        return summaries

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_summarize_with_llm, t[2], t[1]): t[0] for t in tasks}
        for fut in concurrent.futures.as_completed(futures):
            key = futures[fut]
            try:
                result = fut.result()
                if result:
                    summaries[key] = result
            except Exception as e:
                logger.warning("summary future failed for %s: %s", key, e)
    return summaries


def _persist_summaries(decision_id: int, summaries: dict) -> None:
    """Write summaries back into agent_decisions.raw_state under _summaries key."""
    if not summaries:
        return
    conn = store.connect(_db_path())
    row = conn.execute("SELECT raw_state FROM agent_decisions WHERE id = ?", (decision_id,)).fetchone()
    if not row:
        return
    try:
        state = json.loads(row[0] or "{}")
    except Exception:
        state = {}
    state["_summaries"] = summaries
    conn.execute(
        "UPDATE agent_decisions SET raw_state = ? WHERE id = ?",
        (json.dumps(state, default=str), decision_id),
    )
    conn.commit()


def _build_agent_views(state: dict, decision_id: int | None = None) -> list[dict]:
    """Per-agent: structured summary (LLM-generated, cached) + full text fallback."""
    cached_summaries = state.get("_summaries") or {}

    # If we have no cached summaries and we know the decision id, generate them.
    if not cached_summaries and decision_id is not None:
        cached_summaries = _summarize_decision_state(state)
        if cached_summaries:
            _persist_summaries(decision_id, cached_summaries)

    agents = [(k, lbl, e) for k, lbl, e, _ in _AGENT_DEFS]
    views = []
    for key, label, emoji in agents:
        text = state.get(key) or ""
        if not text:
            continue
        llm_summary = cached_summaries.get(key) or {}
        stance = llm_summary.get("stance")
        signal = (
            {"label": stance, "tone": {"Bullish": "bull", "Bearish": "bear", "Neutral": "neutral"}.get(stance, "neutral")}
            if stance
            else _signal_from_text(text)
        )
        views.append({
            "key": key,
            "label": label,
            "emoji": emoji,
            "headline": llm_summary.get("headline") or _extract_verdict(text),
            "reason": llm_summary.get("reason") or _extract_reasoning(text),
            "key_points": llm_summary.get("key_points") or _extract_metrics(text),
            "verdict": llm_summary.get("headline") or _extract_verdict(text),
            "reasoning": llm_summary.get("reason") or _extract_reasoning(text),
            "summary": llm_summary.get("headline") or _extract_verdict(text),
            "signal": signal,
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
    raw_rating = rating_match.group(1).title() if rating_match else None
    display, css = display_rating(raw_rating)
    return {
        "rating": display,            # display label (Buy / Add / Hold / Trim / Sell)
        "rating_raw": raw_rating,     # original upstream string
        "rating_class": css,          # CSS class suffix
        "summary": (summary_match.group(1).strip() if summary_match else "")[:400],
        "price_target": pt_match.group(1) if pt_match else None,
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
    out["agents"] = _build_agent_views(state, decision_id=decision_id)
    out["pm"] = _pm_summary(state)
    return out


def _positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM positions_snapshot "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM positions_snapshot) "
        "ORDER BY market_value DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _live_alpaca_state() -> dict:
    """Pull live cash + open orders from Alpaca. Returns {} on failure."""
    try:
        from portfolio.executor.alpaca_client import AlpacaClient
        client = AlpacaClient(paper=True)
        acct = client.get_account()
        orders = client.list_open_orders()
        return {
            "cash": acct.cash,
            "equity": acct.equity,
            "buying_power": acct.buying_power,
            "portfolio_value": acct.portfolio_value,
            "open_orders": orders,
        }
    except Exception as e:
        logger.warning("live Alpaca fetch failed: %s", e)
        return {}


def _fill_missing_universe(decisions: list[dict]) -> list[dict]:
    """Show all 9 universe tickers; mark ones missing for the latest date as 'errored'.

    If `decisions` is empty (DB has no rows yet — pre-launch state), return
    empty so the dashboard shows the friendly "Awaiting first run" placeholder
    instead of 9 confusing 'failed' ghost cards.
    """
    if not decisions:
        return []
    present = {d["ticker"]: d for d in decisions}
    trade_date = decisions[0]["trade_date"]
    out = []
    for ticker in UNIVERSE:
        if ticker in present:
            out.append(present[ticker])
        else:
            out.append({
                "id": None,
                "ticker": ticker,
                "trade_date": trade_date,
                "action": None,
                "action_display": "No decision",
                "action_class": "errored",
                "errored": True,
                "pm": {},
            })
    return out


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

    # Attach full state + display labels to each decision for client-side modal.
    decisions_full = []
    for d in decisions:
        full = _decision_full(conn, d["id"])
        if full:
            disp, cls = display_rating(full.get("action"))
            full["action_display"] = disp
            full["action_class"] = cls
            decisions_full.append(full)

    # Pad with placeholder entries for any universe ticker missing for this date.
    decisions_full = _fill_missing_universe(decisions_full)

    # Live Alpaca state (cash + pending orders).
    live = _live_alpaca_state()
    live_cash = live.get("cash")
    open_orders = live.get("open_orders") or []

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
        disp, _ = display_rating(d["action"])
        decisions_by_rating[disp] = decisions_by_rating.get(disp, 0) + 1

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
        "cash": live_cash,
        "open_orders": open_orders,
        "open_orders_total": sum(
            (o.get("notional") or 0) for o in open_orders
        ),
        "decisions": decisions_full,
        "decisions_json": json.dumps(decisions_full, default=str),
        "decisions_by_rating": decisions_by_rating,
        "universe": UNIVERSE,
        "cash_park_ticker": CASH_PARK_TICKER,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    vm = _build_view_model()
    html = _jinja.get_template("index.html").render(**vm)
    return HTMLResponse(html)


@app.get("/architecture", response_class=HTMLResponse)
async def architecture():
    html = _jinja.get_template("architecture.html").render()
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
