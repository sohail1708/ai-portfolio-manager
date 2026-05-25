"""Lightweight tests for portfolio.scheduler.runner.

Most of the runner requires live Alpaca + APScheduler so we test only
the pure logic here. End-to-end is validated via --once --dry-run.
"""

from __future__ import annotations

from portfolio.scheduler.runner import parse_universe


def test_parse_universe_default():
    assert parse_universe("") == []


def test_parse_universe_basic():
    assert parse_universe("AAPL,MSFT,NVDA") == ["AAPL", "MSFT", "NVDA"]


def test_parse_universe_trims_and_uppercases():
    assert parse_universe(" aapl , msft ,  nvda ") == ["AAPL", "MSFT", "NVDA"]


def test_parse_universe_drops_empties():
    assert parse_universe("AAPL,,MSFT,") == ["AAPL", "MSFT"]


def test_parse_universe_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("PORTFOLIO_UNIVERSE", "tsla, amd")
    assert parse_universe() == ["TSLA", "AMD"]
