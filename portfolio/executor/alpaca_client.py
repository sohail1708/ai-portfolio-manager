"""Thin wrapper around alpaca-py for the operations the executor needs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest


@dataclass
class Account:
    cash: float
    equity: float
    buying_power: float
    portfolio_value: float


@dataclass
class Position:
    ticker: str
    qty: float
    avg_entry_price: float
    market_value: float
    unrealized_pl: float


@dataclass
class OrderResult:
    alpaca_order_id: str
    status: str
    filled_qty: float
    filled_avg_price: float | None


class AlpacaClient:
    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        paper: bool = True,
    ):
        api_key = api_key or os.environ.get("ALPACA_API_KEY")
        secret_key = secret_key or os.environ.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise RuntimeError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set "
                "(either as args or in .env)."
            )
        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret_key)

    def get_account(self) -> Account:
        a = self._trading.get_account()
        return Account(
            cash=float(a.cash),
            equity=float(a.equity),
            buying_power=float(a.buying_power),
            portfolio_value=float(a.portfolio_value),
        )

    def get_positions(self) -> list[Position]:
        return [_to_position(p) for p in self._trading.get_all_positions()]

    def get_position(self, ticker: str) -> Position | None:
        try:
            p = self._trading.get_open_position(ticker)
        except Exception:
            return None
        return _to_position(p)

    def get_latest_price(self, ticker: str) -> float:
        req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
        quote = self._data.get_stock_latest_quote(req)[ticker]
        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
        if bid and ask:
            return (bid + ask) / 2
        return float(quote.ask_price or quote.bid_price or 0)

    def submit_market_order(
        self,
        ticker: str,
        side: str,
        qty: float | None = None,
        notional: float | None = None,
    ) -> OrderResult:
        if (qty is None) == (notional is None):
            raise ValueError("Provide exactly one of qty or notional.")
        req = MarketOrderRequest(
            symbol=ticker,
            side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            qty=qty,
            notional=notional,
        )
        order = self._trading.submit_order(req)
        return _to_order_result(order)

    def get_order(self, alpaca_order_id: str) -> OrderResult:
        order = self._trading.get_order_by_id(alpaca_order_id)
        return _to_order_result(order)

    def cancel_order(self, alpaca_order_id: str) -> None:
        self._trading.cancel_order_by_id(alpaca_order_id)


def _to_position(p: Any) -> Position:
    return Position(
        ticker=p.symbol,
        qty=float(p.qty),
        avg_entry_price=float(p.avg_entry_price),
        market_value=float(p.market_value),
        unrealized_pl=float(p.unrealized_pl),
    )


def _to_order_result(order: Any) -> OrderResult:
    return OrderResult(
        alpaca_order_id=str(order.id),
        status=str(order.status).lower().split(".")[-1],
        filled_qty=float(order.filled_qty or 0),
        filled_avg_price=(
            float(order.filled_avg_price) if order.filled_avg_price else None
        ),
    )
