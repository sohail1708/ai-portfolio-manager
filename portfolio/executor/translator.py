"""Translate TradingAgents decisions into Alpaca order intents.

Direction (BUY/SELL/HOLD) comes from the LLM; sizing comes from our policy.
The LLM's "Position Sizing" field is free-form text we deliberately ignore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from portfolio.executor.alpaca_client import Position

Action = Literal["BUY", "SELL", "HOLD"]


@dataclass
class ParsedDecision:
    action: Action
    stop_loss: float | None
    reasoning: str | None
    raw_sizing_text: str | None


@dataclass
class TranslatorConfig:
    target_position_pct_nav: float = 0.05
    max_position_pct_nav: float = 0.20
    min_trade_notional: float = 25.0


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


_FINAL_RE = re.compile(
    r"FINAL\s+TRANSACTION\s+PROPOSAL\s*[:：]\s*\*{0,2}\s*(BUY|SELL|HOLD)",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"\*{0,2}Action\*{0,2}\s*[:：]\s*\*{0,2}\s*(Buy|Sell|Hold)",
    re.IGNORECASE,
)
_STOP_LOSS_RE = re.compile(
    r"\*{0,2}Stop\s*Loss\*{0,2}\s*[:：]\s*\*{0,2}\s*\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_SIZING_RE = re.compile(
    r"\*{0,2}Position\s*Sizing\*{0,2}\s*[:：]\s*(.+?)(?=\n\s*\*\*|\nFINAL|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_REASONING_RE = re.compile(
    r"\*{0,2}Reasoning\*{0,2}\s*[:：]\s*(.+?)(?=\n\s*\*\*|\nFINAL|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def parse_decision(raw_text: str) -> ParsedDecision:
    final = _FINAL_RE.search(raw_text)
    action_match = final or _ACTION_RE.search(raw_text)
    if not action_match:
        raise ValueError(
            "Could not find Action or FINAL TRANSACTION PROPOSAL in decision text."
        )
    action = action_match.group(1).upper()
    assert action in ("BUY", "SELL", "HOLD")

    stop = _STOP_LOSS_RE.search(raw_text)
    stop_loss = float(stop.group(1).replace(",", "")) if stop else None

    sizing = _SIZING_RE.search(raw_text)
    raw_sizing = sizing.group(1).strip() if sizing else None

    reasoning = _REASONING_RE.search(raw_text)
    reasoning_text = reasoning.group(1).strip() if reasoning else None

    return ParsedDecision(
        action=action,  # type: ignore[arg-type]
        stop_loss=stop_loss,
        reasoning=reasoning_text,
        raw_sizing_text=raw_sizing,
    )


def translate(
    *,
    ticker: str,
    decision: ParsedDecision,
    ctx: TranslatorContext,
    cfg: TranslatorConfig | None = None,
) -> OrderIntent | None:
    cfg = cfg or TranslatorConfig()

    if decision.action == "HOLD":
        return None

    if decision.action == "BUY":
        return _build_buy(ticker, ctx, cfg)

    if decision.action == "SELL":
        return _build_sell(ticker, ctx)

    return None


def _build_buy(
    ticker: str, ctx: TranslatorContext, cfg: TranslatorConfig
) -> OrderIntent | None:
    if ctx.nav <= 0:
        return None

    current_value = (
        ctx.current_position.market_value if ctx.current_position else 0.0
    )
    current_pct = current_value / ctx.nav

    if current_pct >= cfg.max_position_pct_nav:
        return None  # already at or above cap, don't add

    target_value = cfg.target_position_pct_nav * ctx.nav
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
        reason=f"buy_to_{int(cfg.target_position_pct_nav * 100)}pct_nav",
    )


def _build_sell(ticker: str, ctx: TranslatorContext) -> OrderIntent | None:
    if not ctx.current_position or ctx.current_position.qty <= 0:
        return None  # no position to sell

    return OrderIntent(
        ticker=ticker,
        side="sell",
        qty=ctx.current_position.qty,
        reason="close_full_position",
    )
