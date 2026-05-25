"""Tests for portfolio.executor.translator — parser + sizing policy."""

from __future__ import annotations

import pytest

from portfolio.executor.alpaca_client import Position
from portfolio.executor.translator import (
    OrderIntent,
    ParsedDecision,
    TranslatorConfig,
    TranslatorContext,
    parse_decision,
    translate,
)


# Sample text matching the format we saw in the upstream smoke run.
SAMPLE_BUY = """
**Action**: Buy

**Reasoning**: The plan supports an Overweight stance in NVDA. RSI is mid-50s, MACD positive.

**Stop Loss**: 76.0

**Position Sizing**: Add incrementally toward a 4-5% NAV single-name risk cap; start with 40%.

FINAL TRANSACTION PROPOSAL: **BUY**
"""

SAMPLE_SELL = """
**Action**: Sell

**Reasoning**: Weakening fundamentals.

**Stop Loss**: 145.50

**Position Sizing**: Close full position.

FINAL TRANSACTION PROPOSAL: **SELL**
"""

SAMPLE_HOLD = """
**Action**: Hold

**Reasoning**: Wait for clearer signal.

FINAL TRANSACTION PROPOSAL: **HOLD**
"""


class TestParseDecision:
    def test_buy_full_fields(self):
        d = parse_decision(SAMPLE_BUY)
        assert d.action == "BUY"
        assert d.stop_loss == 76.0
        assert d.reasoning is not None
        assert "Overweight stance" in d.reasoning
        assert d.raw_sizing_text is not None
        assert "4-5% NAV" in d.raw_sizing_text

    def test_sell_with_decimal_stop(self):
        d = parse_decision(SAMPLE_SELL)
        assert d.action == "SELL"
        assert d.stop_loss == 145.50

    def test_hold_no_stop_loss(self):
        d = parse_decision(SAMPLE_HOLD)
        assert d.action == "HOLD"
        assert d.stop_loss is None

    def test_strips_commas_in_stop_loss(self):
        d = parse_decision("FINAL TRANSACTION PROPOSAL: BUY\n**Stop Loss**: 1,234.56")
        assert d.stop_loss == 1234.56

    def test_raises_when_no_action(self):
        with pytest.raises(ValueError):
            parse_decision("no action here")

    def test_falls_back_to_action_line_when_no_final(self):
        # Some upstream variants only emit "**Action**: Buy" without FINAL.
        d = parse_decision("**Action**: Buy\nrationale here")
        assert d.action == "BUY"


class TestTranslateBuy:
    def _ctx(self, nav=100_000, cash=50_000, position=None):
        return TranslatorContext(
            nav=nav, cash=cash, buying_power=cash, current_position=position
        )

    def test_fresh_buy_targets_5pct_of_nav(self):
        decision = ParsedDecision(action="BUY", stop_loss=None, reasoning=None, raw_sizing_text=None)
        intent = translate(ticker="NVDA", decision=decision, ctx=self._ctx())
        assert intent is not None
        assert intent.side == "buy"
        assert intent.notional == 5_000.0
        assert intent.qty is None

    def test_buy_caps_to_buying_power(self):
        decision = ParsedDecision(action="BUY", stop_loss=None, reasoning=None, raw_sizing_text=None)
        intent = translate(
            ticker="NVDA",
            decision=decision,
            ctx=self._ctx(nav=100_000, cash=2_000),
        )
        assert intent is not None
        assert intent.notional == 2_000.0

    def test_buy_skipped_when_already_at_cap(self):
        # Already at 20% (the default cap) of a $100k NAV → no add.
        existing = Position(
            ticker="NVDA", qty=200, avg_entry_price=100, market_value=20_000, unrealized_pl=0
        )
        decision = ParsedDecision(action="BUY", stop_loss=None, reasoning=None, raw_sizing_text=None)
        intent = translate(
            ticker="NVDA", decision=decision, ctx=self._ctx(position=existing)
        )
        assert intent is None

    def test_buy_tops_up_below_target(self):
        # Position is at 3% of NAV; target is 5% → top up $2k.
        existing = Position(
            ticker="NVDA", qty=30, avg_entry_price=100, market_value=3_000, unrealized_pl=0
        )
        decision = ParsedDecision(action="BUY", stop_loss=None, reasoning=None, raw_sizing_text=None)
        intent = translate(
            ticker="NVDA", decision=decision, ctx=self._ctx(position=existing)
        )
        assert intent is not None
        assert intent.notional == 2_000.0

    def test_buy_adds_toward_cap_when_above_target(self):
        # Position at 7% (above 5% target, below 20% cap) → add toward cap.
        existing = Position(
            ticker="NVDA", qty=70, avg_entry_price=100, market_value=7_000, unrealized_pl=0
        )
        decision = ParsedDecision(action="BUY", stop_loss=None, reasoning=None, raw_sizing_text=None)
        intent = translate(
            ticker="NVDA", decision=decision, ctx=self._ctx(position=existing)
        )
        assert intent is not None
        # cap_value (20k) - current (7k) = 13k
        assert intent.notional == 13_000.0

    def test_buy_skipped_below_min_notional(self):
        cfg = TranslatorConfig(target_position_pct_nav=0.05, min_trade_notional=25.0)
        # Position at 4.99% of NAV; top-up would be $10 → below min.
        existing = Position(
            ticker="NVDA", qty=49.9, avg_entry_price=100, market_value=4_990, unrealized_pl=0
        )
        decision = ParsedDecision(action="BUY", stop_loss=None, reasoning=None, raw_sizing_text=None)
        intent = translate(
            ticker="NVDA",
            decision=decision,
            ctx=self._ctx(position=existing),
            cfg=cfg,
        )
        assert intent is None


class TestTranslateSell:
    def _ctx(self, position):
        return TranslatorContext(
            nav=100_000, cash=50_000, buying_power=50_000, current_position=position
        )

    def test_sell_closes_full_position(self):
        existing = Position(
            ticker="NVDA", qty=37.5, avg_entry_price=100, market_value=4_000, unrealized_pl=200
        )
        decision = ParsedDecision(action="SELL", stop_loss=None, reasoning=None, raw_sizing_text=None)
        intent = translate(ticker="NVDA", decision=decision, ctx=self._ctx(existing))
        assert intent is not None
        assert intent.side == "sell"
        assert intent.qty == 37.5
        assert intent.notional is None

    def test_sell_with_no_position_is_noop(self):
        decision = ParsedDecision(action="SELL", stop_loss=None, reasoning=None, raw_sizing_text=None)
        intent = translate(ticker="NVDA", decision=decision, ctx=self._ctx(None))
        assert intent is None


class TestTranslateHold:
    def test_hold_returns_none(self):
        decision = ParsedDecision(action="HOLD", stop_loss=None, reasoning=None, raw_sizing_text=None)
        ctx = TranslatorContext(nav=100_000, cash=50_000, buying_power=50_000, current_position=None)
        assert translate(ticker="NVDA", decision=decision, ctx=ctx) is None
