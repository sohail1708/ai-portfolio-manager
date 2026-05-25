"""Tests for portfolio.executor.translator — 5-tier parser + sizing policy."""

from __future__ import annotations

import pytest

from portfolio.executor.alpaca_client import Position
from portfolio.executor.translator import (
    ParsedDecision,
    TranslatorConfig,
    TranslatorContext,
    parse_decision,
    translate,
)


# Sample matching the Portfolio Manager's actual rendered output
# (tradingagents/agents/schemas.py::render_pm_decision).
SAMPLE_BUY = """
**Rating**: Buy

**Executive Summary**: Strong fundamentals and AI tailwind support full conviction.

**Investment Thesis**: NVDA dominates AI hardware. Margins remain elite at 65%.

**Price Target**: 165.50

**Time Horizon**: 3-6 months
"""

SAMPLE_OVERWEIGHT = """
**Rating**: Overweight

**Executive Summary**: Favorable outlook, incremental add.

**Investment Thesis**: Solid setup but valuation rich.
"""

SAMPLE_HOLD = """
**Rating**: Hold

**Executive Summary**: Wait for clearer signal.

**Investment Thesis**: Mixed indicators.
"""

SAMPLE_UNDERWEIGHT = """
**Rating**: Underweight

**Executive Summary**: Weakening signals; trim exposure.

**Investment Thesis**: Tighter margins ahead.
"""

SAMPLE_SELL = """
**Rating**: Sell

**Executive Summary**: Exit position.

**Investment Thesis**: Multiple warning signs.
"""


class TestParseDecision:
    def test_buy_full_fields(self):
        d = parse_decision(SAMPLE_BUY)
        assert d.rating == "Buy"
        assert d.executive_summary is not None
        assert "Strong fundamentals" in d.executive_summary
        assert d.investment_thesis is not None
        assert "NVDA dominates" in d.investment_thesis
        assert d.price_target == 165.50
        assert d.time_horizon == "3-6 months"

    def test_overweight_no_price_target(self):
        d = parse_decision(SAMPLE_OVERWEIGHT)
        assert d.rating == "Overweight"
        assert d.price_target is None
        assert d.time_horizon is None

    def test_hold(self):
        d = parse_decision(SAMPLE_HOLD)
        assert d.rating == "Hold"

    def test_underweight(self):
        d = parse_decision(SAMPLE_UNDERWEIGHT)
        assert d.rating == "Underweight"

    def test_sell(self):
        d = parse_decision(SAMPLE_SELL)
        assert d.rating == "Sell"

    def test_defaults_to_hold_when_no_rating(self):
        # Upstream's parse_rating defaults to "Hold" rather than raising.
        d = parse_decision("no rating mentioned")
        assert d.rating == "Hold"

    def test_strips_commas_in_price_target(self):
        d = parse_decision("**Rating**: Buy\n**Price Target**: 1,234.56")
        assert d.price_target == 1234.56


class TestTranslateBuy:
    def _ctx(self, nav=100_000, cash=50_000, position=None):
        return TranslatorContext(
            nav=nav, cash=cash, buying_power=cash, current_position=position
        )

    def _decision(self, rating):
        return ParsedDecision(
            rating=rating,
            executive_summary=None,
            investment_thesis=None,
            price_target=None,
            time_horizon=None,
        )

    def test_fresh_buy_targets_5pct_of_nav(self):
        intent = translate(ticker="NVDA", decision=self._decision("Buy"), ctx=self._ctx())
        assert intent is not None
        assert intent.side == "buy"
        assert intent.notional == 5_000.0
        assert "buy" in intent.reason

    def test_fresh_overweight_targets_2_5pct_of_nav(self):
        intent = translate(ticker="NVDA", decision=self._decision("Overweight"), ctx=self._ctx())
        assert intent is not None
        assert intent.notional == 2_500.0
        assert "overweight" in intent.reason

    def test_buy_caps_to_buying_power(self):
        intent = translate(
            ticker="NVDA",
            decision=self._decision("Buy"),
            ctx=self._ctx(nav=100_000, cash=2_000),
        )
        assert intent is not None
        assert intent.notional == 2_000.0

    def test_buy_skipped_when_already_at_cap(self):
        existing = Position(
            ticker="NVDA", qty=200, avg_entry_price=100, market_value=20_000, unrealized_pl=0
        )
        intent = translate(
            ticker="NVDA", decision=self._decision("Buy"), ctx=self._ctx(position=existing)
        )
        assert intent is None

    def test_overweight_above_target_adds_toward_cap(self):
        # Position at 7% (above 2.5% overweight target, below 20% cap) → top up to cap.
        existing = Position(
            ticker="NVDA", qty=70, avg_entry_price=100, market_value=7_000, unrealized_pl=0
        )
        intent = translate(
            ticker="NVDA", decision=self._decision("Overweight"), ctx=self._ctx(position=existing)
        )
        assert intent is not None
        assert intent.notional == 13_000.0  # cap (20k) - current (7k)

    def test_buy_skipped_below_min_notional(self):
        cfg = TranslatorConfig(buy_pct_nav=0.05, min_trade_notional=25.0)
        existing = Position(
            ticker="NVDA", qty=49.9, avg_entry_price=100, market_value=4_990, unrealized_pl=0
        )
        intent = translate(
            ticker="NVDA",
            decision=self._decision("Buy"),
            ctx=self._ctx(position=existing),
            cfg=cfg,
        )
        assert intent is None


class TestTranslateSell:
    def _ctx(self, position):
        return TranslatorContext(
            nav=100_000, cash=50_000, buying_power=50_000, current_position=position
        )

    def _decision(self, rating):
        return ParsedDecision(
            rating=rating,
            executive_summary=None,
            investment_thesis=None,
            price_target=None,
            time_horizon=None,
        )

    def test_sell_closes_full_position(self):
        existing = Position(
            ticker="NVDA", qty=37.5, avg_entry_price=100, market_value=4_000, unrealized_pl=200
        )
        intent = translate(ticker="NVDA", decision=self._decision("Sell"), ctx=self._ctx(existing))
        assert intent is not None
        assert intent.side == "sell"
        assert intent.qty == 37.5
        assert intent.notional is None

    def test_underweight_trims_half_by_default(self):
        existing = Position(
            ticker="NVDA", qty=40, avg_entry_price=100, market_value=4_000, unrealized_pl=0
        )
        intent = translate(ticker="NVDA", decision=self._decision("Underweight"), ctx=self._ctx(existing))
        assert intent is not None
        assert intent.side == "sell"
        assert intent.qty == 20.0
        assert "trim" in intent.reason

    def test_sell_with_no_position_is_noop(self):
        intent = translate(ticker="NVDA", decision=self._decision("Sell"), ctx=self._ctx(None))
        assert intent is None

    def test_underweight_with_no_position_is_noop(self):
        intent = translate(ticker="NVDA", decision=self._decision("Underweight"), ctx=self._ctx(None))
        assert intent is None


class TestTranslateHold:
    def test_hold_returns_none(self):
        ctx = TranslatorContext(nav=100_000, cash=50_000, buying_power=50_000, current_position=None)
        decision = ParsedDecision(
            rating="Hold", executive_summary=None, investment_thesis=None,
            price_target=None, time_horizon=None,
        )
        assert translate(ticker="NVDA", decision=decision, ctx=ctx) is None
