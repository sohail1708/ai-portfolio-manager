"""Translate TradingAgents Portfolio Manager decisions into Alpaca order intents.

The upstream Portfolio Manager emits a 5-tier rating (Buy / Overweight / Hold /
Underweight / Sell) along with thesis text. Direction + conviction come from
the LLM; sizing comes from our policy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from portfolio.executor.alpaca_client import Position
from tradingagents.agents.utils.rating import parse_rating

Rating = Literal["Buy", "Overweight", "Hold", "Underweight", "Sell"]


@dataclass
class ParsedDecision:
    rating: Rating
    executive_summary: str | None
    investment_thesis: str | None
    price_target: float | None
    time_horizon: str | None


@dataclass
class TranslatorConfig:
    # Sizing per rating, as a fraction of NAV.
    buy_pct_nav: float = 0.05         # full conviction
    overweight_pct_nav: float = 0.025  # half-position incremental add
    max_position_pct_nav: float = 0.20
    min_trade_notional: float = 25.0
    # Underweight trims this fraction of the current position.
    underweight_trim_fraction: float = 0.50


@dataclass
class TranslatorContext:
    nav: float
    cash: float
    buying_power: float
    current_position: Position | None


@dataclass
class OrderIntent:
    ticker: str
    side: Literal["buy", "sell"]
    notional: float | None = None
    qty: float | None = None
    reason: str = ""


_SECTION_RE_TEMPLATES = {
    "executive_summary": r"\*{0,2}Executive\s*Summary\*{0,2}\s*[:：]\s*(.+?)(?=\n\s*\*\*|\Z)",
    "investment_thesis": r"\*{0,2}Investment\s*Thesis\*{0,2}\s*[:：]\s*(.+?)(?=\n\s*\*\*|\Z)",
    "time_horizon": r"\*{0,2}Time\s*Horizon\*{0,2}\s*[:：]\s*(.+?)(?=\n\s*\*\*|\Z)",
}
_PRICE_TARGET_RE = re.compile(
    r"\*{0,2}Price\s*Target\*{0,2}\s*[:：]\s*\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_SECTION_RES = {
    name: re.compile(pat, re.IGNORECASE | re.DOTALL)
    for name, pat in _SECTION_RE_TEMPLATES.items()
}


def parse_decision(raw_text: str) -> ParsedDecision:
    rating = parse_rating(raw_text)
    if rating not in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
        raise ValueError(f"Unexpected rating from upstream: {rating!r}")

    sections: dict[str, str | None] = {}
    for name, pattern in _SECTION_RES.items():
        m = pattern.search(raw_text)
        sections[name] = m.group(1).strip() if m else None

    pt_match = _PRICE_TARGET_RE.search(raw_text)
    price_target = float(pt_match.group(1).replace(",", "")) if pt_match else None

    return ParsedDecision(
        rating=rating,  # type: ignore[arg-type]
        executive_summary=sections["executive_summary"],
        investment_thesis=sections["investment_thesis"],
        price_target=price_target,
        time_horizon=sections["time_horizon"],
    )


def translate(
    *,
    ticker: str,
    decision: ParsedDecision,
    ctx: TranslatorContext,
    cfg: TranslatorConfig | None = None,
) -> OrderIntent | None:
    cfg = cfg or TranslatorConfig()

    if decision.rating == "Hold":
        return None

    if decision.rating in ("Buy", "Overweight"):
        return _build_buy(ticker, decision.rating, ctx, cfg)

    if decision.rating == "Sell":
        return _build_full_sell(ticker, ctx)

    if decision.rating == "Underweight":
        return _build_trim(ticker, ctx, cfg)

    return None


def _build_buy(
    ticker: str,
    rating: Rating,
    ctx: TranslatorContext,
    cfg: TranslatorConfig,
) -> OrderIntent | None:
    if ctx.nav <= 0:
        return None

    current_value = (
        ctx.current_position.market_value if ctx.current_position else 0.0
    )
    current_pct = current_value / ctx.nav

    if current_pct >= cfg.max_position_pct_nav:
        return None

    target_pct = (
        cfg.buy_pct_nav if rating == "Buy" else cfg.overweight_pct_nav
    )
    target_value = target_pct * ctx.nav
    cap_value = cfg.max_position_pct_nav * ctx.nav

    if current_value >= target_value:
        notional = max(0.0, cap_value - current_value)
    else:
        notional = target_value - current_value

    notional = min(notional, ctx.buying_power)

    if notional < cfg.min_trade_notional:
        return None

    return OrderIntent(
        ticker=ticker,
        side="buy",
        notional=round(notional, 2),
        reason=f"{rating.lower()}_to_{int(target_pct * 100)}pct_nav",
    )


def _build_full_sell(ticker: str, ctx: TranslatorContext) -> OrderIntent | None:
    if not ctx.current_position or ctx.current_position.qty <= 0:
        return None
    return OrderIntent(
        ticker=ticker,
        side="sell",
        qty=ctx.current_position.qty,
        reason="sell_close_full",
    )


def _build_trim(
    ticker: str, ctx: TranslatorContext, cfg: TranslatorConfig
) -> OrderIntent | None:
    if not ctx.current_position or ctx.current_position.qty <= 0:
        return None
    qty = round(ctx.current_position.qty * cfg.underweight_trim_fraction, 4)
    if qty <= 0:
        return None
    # Skip dust trims by approximate notional.
    approx_notional = qty * ctx.current_position.avg_entry_price
    if approx_notional < cfg.min_trade_notional:
        return None
    return OrderIntent(
        ticker=ticker,
        side="sell",
        qty=qty,
        reason=f"underweight_trim_{int(cfg.underweight_trim_fraction * 100)}pct",
    )
