"""
Alpaca Client - Trading and market data via alpaca-py SDK.
Supports paper/live trading, fractional shares, extended hours.
"""

import json
import time
import uuid
from pathlib import Path
import requests
from typing import Dict, List, Optional, Union
from loguru import logger

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    GetOrdersRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockSnapshotRequest

from config import settings


class AlpacaClient:
    """Wrapper around alpaca-py for stock trading."""

    def __init__(self):
        self.api_key = settings.ALPACA_API_KEY
        self.secret_key = settings.ALPACA_SECRET_KEY
        self.paper = settings.ALPACA_PAPER
        self._trading_client: Optional[TradingClient] = None
        self._data_client: Optional[StockHistoricalDataClient] = None
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize Alpaca trading and data clients."""
        if not self.api_key or not self.secret_key:
            logger.error("ALPACA_API_KEY or ALPACA_SECRET_KEY not set")
            return False
        try:
            self._trading_client = TradingClient(
                api_key=self.api_key,
                secret_key=self.secret_key,
                paper=self.paper,
            )
            self._data_client = StockHistoricalDataClient(
                api_key=self.api_key,
                secret_key=self.secret_key,
            )
            # Verify connection
            acct = self._trading_client.get_account()
            if acct.trading_blocked:
                logger.error("Alpaca account is blocked from trading")
                return False
            self._initialized = True
            logger.success(f"Alpaca client initialized ({'paper' if self.paper else 'LIVE'}) — equity: ${float(acct.equity):,.2f}")
            return True
        except Exception as e:
            logger.error(f"Alpaca init failed: {e}")
            return False

    def _ensure_init(self):
        if not self._initialized:
            raise RuntimeError("Alpaca client not initialized. Call initialize() first.")

    def _capture_payload(self, label: str, data):
        """Write a raw payload to data/alpaca_capture/ when capture mode is on."""
        if not getattr(settings, "CAPTURE_ALPACA_PAYLOADS", False):
            return
        try:
            capture_dir = Path(__file__).resolve().parent.parent.parent / "data" / "alpaca_capture"
            capture_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            filepath = capture_dir / f"rest_{label}_{ts}.json"
            filepath.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            logger.debug(f"Payload capture failed ({label}): {e}")

    # ── Account ────────────────────────────────────────────────────

    def get_account(self) -> Dict:
        """Get account info: balance, buying_power, equity."""
        self._ensure_init()
        try:
            acct = self._trading_client.get_account()
            return {
                "cash": float(acct.cash),
                "buying_power": float(acct.buying_power),
                "equity": float(acct.equity),
                "portfolio_value": float(acct.portfolio_value),
                "pattern_day_trader": acct.pattern_day_trader,
                "daytrade_count": acct.daytrade_count,
                "trading_blocked": acct.trading_blocked,
                "long_market_value": float(acct.long_market_value),
            }
        except Exception as e:
            logger.error(f"Get account failed: {e}")
            return {
                "cash": 0.0,
                "buying_power": 0.0,
                "equity": 0.0,
                "portfolio_value": 0.0,
                "pattern_day_trader": False,
                "daytrade_count": 0,
                "trading_blocked": False,
                "long_market_value": 0.0,
            }

    def get_balances(self) -> Dict:
        """Convenience method matching old interface."""
        acct = self.get_account()
        return {
            "cash": acct.get("cash", 0),
            "buying_power": acct.get("buying_power", 0),
            "total_value": acct.get("equity", 0),
        }

    # ── Positions ──────────────────────────────────────────────────

    def get_positions(self) -> List[Dict]:
        """Get all open positions."""
        self._ensure_init()
        try:
            positions = self._trading_client.get_all_positions()
            result = []
            for p in positions:
                raw_qty = float(p.qty)
                side = str(getattr(p, "side", "")).lower()
                if side not in ("long", "short"):
                    side = "short" if raw_qty < 0 else "long"
                qty = abs(raw_qty)
                result.append({
                    "symbol": p.symbol,
                    "quantity": qty,
                    "side": side,
                    "average_price": float(p.avg_entry_price),
                    "current_price": float(p.current_price),
                    "market_value": float(p.market_value),
                    "unrealized_pnl": float(p.unrealized_pl),
                    "unrealized_pnl_pct": float(p.unrealized_plpc) * 100,
                    "open_pnl": float(p.unrealized_pl),
                })
            self._capture_payload("positions", result)
            return result
        except Exception as e:
            logger.error(f"Get positions failed: {e}")
            return []

    # ── Orders ─────────────────────────────────────────────────────

    def place_market_buy(self, symbol: str, qty_or_notional: Union[int, float], force_notional: bool = False) -> Optional[Dict]:
        """
        Place a market buy order.
        If force_notional=True or value is float, treat as dollar amount.
        If int, treat as share quantity.
        """
        self._ensure_init()
        try:
            is_notional = force_notional or (isinstance(qty_or_notional, float))
            if is_notional:
                req = MarketOrderRequest(
                    symbol=symbol,
                    notional=round(qty_or_notional, 2),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=str(uuid.uuid4()),
                )
            else:
                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=int(qty_or_notional),
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=str(uuid.uuid4()),
                )
            order = self._trading_client.submit_order(req)
            logger.success(f"Market BUY: {qty_or_notional} {symbol} → order {order.id}")
            return self._order_to_dict(order)
        except Exception as e:
            # Retry with integer qty if "not fractionable"
            if "not fractionable" in str(e).lower() and is_notional:
                try:
                    # Estimate qty from notional / approximate price
                    # Get latest price to calculate shares
                    price = self.get_latest_price(symbol)
                    if price > 0:
                        int_qty = max(1, int(qty_or_notional / price))
                        req = MarketOrderRequest(
                            symbol=symbol, qty=int_qty, side=OrderSide.BUY,
                            time_in_force=TimeInForce.DAY, client_order_id=str(uuid.uuid4()))
                        order = self._trading_client.submit_order(req)
                        logger.success(f"Market BUY (int qty fallback): {int_qty} {symbol} → order {order.id}")
                        return self._order_to_dict(order)
                except Exception as e2:
                    logger.error(f"Market buy int-qty fallback failed ({symbol}): {e2}")
            logger.error(f"Market buy failed ({symbol}): {e}")
            return None

    def place_market_sell(
        self,
        symbol: str,
        qty_or_notional: Union[int, float],
        _retry_on_qty_conflict: bool = True,
    ) -> Optional[Dict]:
        """Place a market sell order."""
        self._ensure_init()
        try:
            is_notional = isinstance(qty_or_notional, float) and (qty_or_notional != int(qty_or_notional) or qty_or_notional < 1)
            if is_notional:
                req = MarketOrderRequest(
                    symbol=symbol,
                    notional=round(qty_or_notional, 2),
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=str(uuid.uuid4()),
                )
            else:
                req = MarketOrderRequest(
                    symbol=symbol,
                    qty=int(qty_or_notional) if qty_or_notional == int(qty_or_notional) else qty_or_notional,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=str(uuid.uuid4()),
                )
            order = self._trading_client.submit_order(req)
            logger.success(f"Market SELL: {qty_or_notional} {symbol} → order {order.id}")
            return self._order_to_dict(order)
        except Exception as e:
            if _retry_on_qty_conflict and self._is_qty_conflict_error(e):
                cancelled = self.cancel_open_sells_for_symbol(symbol)
                if cancelled:
                    time.sleep(0.5)
                    return self.place_market_sell(symbol, qty_or_notional, _retry_on_qty_conflict=False)
            logger.error(f"Market sell failed ({symbol}): {e}")
            return None

    def place_limit_buy(
        self,
        symbol: str,
        qty: int,
        price: float,
        extended_hours: bool = False,
        _retry_on_qty_conflict: bool = True,
    ) -> Optional[Dict]:
        """Place a limit buy order. Extended hours requires limit+day."""
        self._ensure_init()
        try:
            # Round price per Alpaca rules
            limit_price = round(price, 2) if price >= 1.0 else round(price, 4)
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                extended_hours=extended_hours,
                client_order_id=str(uuid.uuid4()),
            )
            order = self._trading_client.submit_order(req)
            logger.success(f"Limit BUY: {qty} {symbol} @ ${limit_price} → order {order.id}")
            return self._order_to_dict(order)
        except Exception as e:
            if _retry_on_qty_conflict and self._is_qty_conflict_error(e):
                cancelled = self.cancel_open_sells_for_symbol(symbol)
                if cancelled:
                    time.sleep(0.5)
                    return self.place_limit_buy(
                        symbol,
                        qty,
                        price,
                        extended_hours=extended_hours,
                        _retry_on_qty_conflict=False,
                    )
            logger.error(f"Limit buy failed ({symbol}): {e}")
            return None

    def replace_order(self, order_id: str, limit_price: float) -> Optional[Dict]:
        """Replace an open limit order with a new limit price."""
        self._ensure_init()
        try:
            import requests
            resp = requests.patch(
                f"{self._base_url}/v2/orders/{order_id}",
                headers=self._rest_headers(),
                json={"limit_price": str(round(limit_price, 2))},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info(f"Order replaced: {order_id} → new limit ${limit_price:.2f}")
            return data
        except Exception as e:
            logger.error(f"Replace order failed ({order_id}): {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        self._ensure_init()
        try:
            self._trading_client.cancel_order_by_id(order_id)
            logger.info(f"Order cancelled: {order_id}")
            return True
        except Exception as e:
            logger.error(f"Cancel order failed ({order_id}): {e}")
            return False

    def get_orders(self, status: str = "open") -> List[Dict]:
        """Get orders by status: open, closed, all."""
        self._ensure_init()
        try:
            status_map = {
                "open": QueryOrderStatus.OPEN,
                "closed": QueryOrderStatus.CLOSED,
                "all": QueryOrderStatus.ALL,
            }
            req = GetOrdersRequest(
                status=status_map.get(status, QueryOrderStatus.OPEN),
                limit=100,
                nested=True,
            )
            orders = self._trading_client.get_orders(req)
            result = [self._order_to_dict(o) for o in orders]
            self._capture_payload(f"orders_{status}", result)
            return result
        except Exception as e:
            logger.error(f"Get orders failed: {e}")
            return []

    @staticmethod
    def _is_qty_conflict_error(message: object) -> bool:
        text = str(message or "").lower()
        return (
            "insufficient qty" in text
            or "insufficient quantity" in text
            or "insufficient qty available" in text
            or "qty available" in text
        )

    def _get_open_orders_for_symbol(self, symbol: str) -> List[Dict]:
        self._ensure_init()
        try:
            resp = requests.get(
                f"{self._base_url}/v2/orders",
                headers=self._rest_headers(),
                params={
                    "status": "open",
                    "symbols": symbol,
                    "limit": 100,
                    "nested": "true",
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.error(f"Open orders query failed for {symbol}: {resp.status_code} {resp.text[:200]}")
                return []
            rows = resp.json()
            return rows if isinstance(rows, list) else []
        except Exception as e:
            logger.error(f"Open orders query error for {symbol}: {e}")
            return []

    def _wait_for_open_orders_cleared(self, symbol: str, side: str, timeout_seconds: float = 2.0) -> bool:
        deadline = time.time() + max(0.1, float(timeout_seconds or 0))
        target_side = str(side or "").lower()
        while time.time() < deadline:
            open_orders = self._get_open_orders_for_symbol(symbol)
            conflicts = [
                order for order in open_orders
                if str(order.get("side", "")).lower() == target_side
            ]
            if not conflicts:
                return True
            time.sleep(0.2)
        return False

    def _cancel_open_orders_for_symbol(self, symbol: str, side: str) -> int:
        cancelled = 0
        for order in self._get_open_orders_for_symbol(symbol):
            if str(order.get("side", "")).lower() != str(side or "").lower():
                continue
            order_id = str(order.get("id", "") or "").strip()
            if not order_id:
                continue
            if self.cancel_order(order_id):
                cancelled += 1
        if cancelled:
            self._wait_for_open_orders_cleared(symbol, side)
        return cancelled

    def cancel_open_sells_for_symbol(self, symbol: str) -> int:
        """Cancel open sell-side orders that lock a long position."""
        cancelled = self._cancel_open_orders_for_symbol(symbol, "sell")
        if cancelled:
            logger.info(f"Cancelled {cancelled} open sell orders for {symbol}")
        return cancelled

    def cancel_open_buys_for_symbol(self, symbol: str) -> int:
        """Cancel open buy-side orders that conflict with short-cover protection."""
        cancelled = self._cancel_open_orders_for_symbol(symbol, "buy")
        if cancelled:
            logger.info(f"Cancelled {cancelled} open buy orders for {symbol}")
        return cancelled

    # ── Smart Order Execution ─────────────────────────────────────

    def smart_buy(self, symbol: str, notional: float, timeout_seconds: int = 10) -> Optional[Dict]:
        """
        Smart buy: for orders > $500, use limit at mid-price first.
        If not filled in timeout_seconds, cancel and retry at ask.
        For smaller orders, use market order directly.
        """
        if notional <= 500:
            return self.place_market_buy(symbol, notional, force_notional=True)

        self._ensure_init()
        try:
            # Get current quote for mid-price
            price = self.get_price(symbol)
            if price <= 0:
                return self.place_market_buy(symbol, notional)

            qty = int(notional / price)
            if qty <= 0:
                return self.place_market_buy(symbol, notional)

            # Round limit price per Alpaca rules
            limit_price = round(price, 2) if price >= 1.0 else round(price, 4)

            # Try limit at current price
            order = self.place_limit_buy(symbol, qty, limit_price)
            if not order:
                return self.place_market_buy(symbol, notional)

            # Wait for fill
            import time as _time
            order_id = order.get("id", "")
            deadline = _time.time() + timeout_seconds
            while _time.time() < deadline:
                _time.sleep(1)
                try:
                    live_order = self._trading_client.get_order_by_id(order_id)
                    if str(live_order.status) in ("filled", "partially_filled"):
                        return self._order_to_dict(live_order)
                except Exception:
                    break

            # Not filled — cancel and use market
            self.cancel_order(order_id)
            logger.info(f"Smart buy: limit not filled for {symbol}, falling back to market")
            return self.place_market_buy(symbol, notional, force_notional=True)

        except Exception as e:
            logger.error(f"Smart buy failed ({symbol}), falling back to market: {e}")
            return self.place_market_buy(symbol, notional, force_notional=True)

    def smart_sell(self, symbol: str, qty: float, timeout_seconds: int = 10) -> Optional[Dict]:
        """
        Smart sell: for position value > $500, try limit at current price first.
        Falls back to market order.
        """
        price = self.get_price(symbol)
        if price * qty <= 500 or price <= 0:
            return self.place_market_sell(symbol, qty)

        self._ensure_init()
        try:
            limit_price = round(price, 2) if price >= 1.0 else round(price, 4)
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty if qty != int(qty) else int(qty),
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                client_order_id=str(uuid.uuid4()),
            )
            order = self._trading_client.submit_order(req)
            order_id = str(order.id)

            import time as _time
            deadline = _time.time() + timeout_seconds
            while _time.time() < deadline:
                _time.sleep(1)
                try:
                    live_order = self._trading_client.get_order_by_id(order_id)
                    if str(live_order.status) in ("filled", "partially_filled"):
                        return self._order_to_dict(live_order)
                except Exception:
                    break

            self.cancel_order(order_id)
            logger.info(f"Smart sell: limit not filled for {symbol}, falling back to market")
            return self.place_market_sell(symbol, qty)

        except Exception as e:
            logger.error(f"Smart sell failed ({symbol}), falling back to market: {e}")
            return self.place_market_sell(symbol, qty)

    # ── Bracket & Trailing Stop Orders ───────────────────────────

    def place_bracket_buy(self, symbol: str, qty: int, limit_price: float,
                          stop_loss_pct: float = 3.0, take_profit_pct: float = None,
                          extended_hours: bool = False) -> Optional[Dict]:
        """
        Place a bracket order: BUY + stop loss + optional take profit.
        Broker-side protection — executes even if bot crashes.
        
        Args:
            symbol: Stock ticker
            qty: Number of shares (whole only for bracket)
            limit_price: Limit price for the buy
            stop_loss_pct: Stop loss percentage below entry (default 3%)
            take_profit_pct: Take profit percentage above entry (optional)
            extended_hours: Whether to allow extended hours
        """
        self._ensure_init()
        if qty < 1:
            logger.warning(f"Bracket orders require whole shares, got {qty}. Using market buy instead.")
            return self.place_limit_buy(symbol, max(1, int(qty)), limit_price, extended_hours)

        try:
            stop_price = round(limit_price * (1 - stop_loss_pct / 100), 2)
            
            order_data = {
                'symbol': symbol,
                'qty': str(qty),
                'side': 'buy',
                'type': 'limit',
                'limit_price': str(round(limit_price, 2)),
                'time_in_force': 'gtc',
                'order_class': 'bracket' if take_profit_pct else 'oto',
                'stop_loss': {
                    'stop_price': str(stop_price),
                },
            }
            
            if take_profit_pct:
                tp_price = round(limit_price * (1 + take_profit_pct / 100), 2)
                order_data['take_profit'] = {'limit_price': str(tp_price)}

            resp = requests.post(
                f'{self._base_url}/v2/orders',
                headers=self._rest_headers(),
                json=order_data,
                timeout=10,
            )
            
            if resp.status_code in (200, 201):
                data = resp.json()
                legs = data.get('legs', [])
                logger.success(
                    f"🛡️ Bracket order: BUY {qty} {symbol} @ ${limit_price:.2f} "
                    f"stop=${stop_price:.2f} ({stop_loss_pct}%)"
                    f"{f' tp=${tp_price:.2f}' if take_profit_pct else ''} "
                    f"[{len(legs)} legs]"
                )
                return {
                    'id': data['id'],
                    'symbol': symbol,
                    'side': 'buy',
                    'type': 'bracket',
                    'qty': str(qty),
                    'limit_price': str(limit_price),
                    'stop_price': str(stop_price),
                    'status': data['status'],
                    'order_class': data.get('order_class', ''),
                    'legs': legs,
                }
            else:
                logger.error(f"Bracket order failed: {resp.status_code} {resp.text[:200]}")
                # Fallback to simple limit buy
                return self.place_limit_buy(symbol, int(qty), limit_price, extended_hours)

        except Exception as e:
            logger.error(f"Bracket order error for {symbol}: {e}")
            return self.place_limit_buy(symbol, int(qty), limit_price, extended_hours)

    def place_bracket_short(self, symbol: str, qty: int, limit_price: float,
                            stop_loss_pct: float = 3.0, take_profit_pct: float = None) -> Optional[Dict]:
        """
        Place a bracket SHORT order: SELL SHORT + stop loss (buy to cover) + optional take profit.
        
        Args:
            symbol: Stock ticker
            qty: Number of shares to short
            limit_price: Limit price for the short sale
            stop_loss_pct: Stop loss percentage ABOVE entry (covers the short)
            take_profit_pct: Take profit percentage BELOW entry
        """
        self._ensure_init()
        if qty < 1:
            logger.warning(f"Bracket short requires whole shares, got {qty}")
            return None

        try:
            stop_price = round(limit_price * (1 + stop_loss_pct / 100), 2)  # ABOVE for shorts

            order_data = {
                'symbol': symbol,
                'qty': str(qty),
                'side': 'sell',
                'type': 'limit',
                'limit_price': str(round(limit_price, 2)),
                'time_in_force': 'gtc',
                'order_class': 'bracket' if take_profit_pct else 'oto',
                'stop_loss': {
                    'stop_price': str(stop_price),
                },
            }

            if take_profit_pct:
                tp_price = round(limit_price * (1 - take_profit_pct / 100), 2)  # BELOW for shorts
                order_data['take_profit'] = {'limit_price': str(tp_price)}

            resp = requests.post(
                f'{self._base_url}/v2/orders',
                headers=self._rest_headers(),
                json=order_data,
                timeout=10,
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                legs = data.get('legs', [])
                logger.success(
                    f"🩳 Bracket SHORT: SELL {qty} {symbol} @ ${limit_price:.2f} "
                    f"stop=${stop_price:.2f} ({stop_loss_pct}%) [{len(legs)} legs]"
                )
                return {
                    'id': data['id'],
                    'symbol': symbol,
                    'side': 'sell',
                    'type': 'bracket',
                    'qty': str(qty),
                    'limit_price': str(limit_price),
                    'stop_price': str(stop_price),
                    'status': data['status'],
                    'order_class': data.get('order_class', ''),
                    'legs': legs,
                    'is_short': True,
                }
            else:
                logger.error(f"Bracket short failed: {resp.status_code} {resp.text[:200]}")
                return None

        except Exception as e:
            logger.error(f"Bracket short error for {symbol}: {e}")
            return None

    def place_trailing_stop(
        self,
        symbol: str,
        qty,
        trail_percent: float = 3.0,
        _retry_on_qty_conflict: bool = True,
    ) -> Optional[Dict]:
        """
        Place a trailing stop sell order on an existing position.
        The stop price follows the stock up and triggers on a pullback.
        
        Args:
            symbol: Stock ticker
            qty: Number of shares to sell
            trail_percent: Percentage below high-water mark to trigger (default 3%)
        """
        self._ensure_init()
        try:
            order_data = {
                'symbol': symbol,
                'qty': str(qty),
                'side': 'sell',
                'type': 'trailing_stop',
                'trail_percent': str(trail_percent),
                'time_in_force': 'gtc',
            }

            resp = requests.post(
                f'{self._base_url}/v2/orders',
                headers=self._rest_headers(),
                json=order_data,
                timeout=10,
            )
            
            if resp.status_code in (200, 201):
                data = resp.json()
                logger.success(
                    f"📈 Trailing stop: {symbol} {qty}sh trail={trail_percent}% "
                    f"hwm={data.get('hwm', '?')} stop={data.get('stop_price', 'tracking')}"
                )
                return {
                    'id': data['id'],
                    'symbol': symbol,
                    'side': 'sell',
                    'type': 'trailing_stop',
                    'qty': str(qty),
                    'trail_percent': str(trail_percent),
                    'stop_price': data.get('stop_price'),
                    'hwm': data.get('hwm'),
                    'status': data['status'],
                }
            if _retry_on_qty_conflict and self._is_qty_conflict_error(resp.text):
                cancelled = self.cancel_open_sells_for_symbol(symbol)
                if cancelled:
                    time.sleep(0.5)
                    return self.place_trailing_stop(
                        symbol,
                        qty,
                        trail_percent=trail_percent,
                        _retry_on_qty_conflict=False,
                    )
            logger.error(f"Trailing stop failed: {resp.status_code} {resp.text[:200]}")
            return None

        except Exception as e:
            if _retry_on_qty_conflict and self._is_qty_conflict_error(e):
                cancelled = self.cancel_open_sells_for_symbol(symbol)
                if cancelled:
                    time.sleep(0.5)
                    return self.place_trailing_stop(
                        symbol,
                        qty,
                        trail_percent=trail_percent,
                        _retry_on_qty_conflict=False,
                    )
            logger.error(f"Trailing stop error for {symbol}: {e}")
            return None

    def place_trailing_stop_short(
        self,
        symbol: str,
        qty,
        trail_percent: float = 3.0,
        _retry_on_qty_conflict: bool = True,
    ) -> Optional[Dict]:
        """
        Place a trailing stop BUY order for a short position (buy-to-cover).
        """
        self._ensure_init()
        try:
            order_data = {
                'symbol': symbol,
                'qty': str(qty),
                'side': 'buy',
                'type': 'trailing_stop',
                'trail_percent': str(trail_percent),
                'time_in_force': 'gtc',
            }

            resp = requests.post(
                f'{self._base_url}/v2/orders',
                headers=self._rest_headers(),
                json=order_data,
                timeout=10,
            )

            if resp.status_code in (200, 201):
                data = resp.json()
                logger.success(
                    f"📉 Short trailing stop: {symbol} {qty}sh trail={trail_percent}% "
                    f"hwm={data.get('hwm', '?')} stop={data.get('stop_price', 'tracking')}"
                )
                return {
                    'id': data['id'],
                    'symbol': symbol,
                    'side': 'buy',
                    'type': 'trailing_stop',
                    'qty': str(qty),
                    'trail_percent': str(trail_percent),
                    'stop_price': data.get('stop_price'),
                    'hwm': data.get('hwm'),
                    'status': data['status'],
                }

            if _retry_on_qty_conflict and self._is_qty_conflict_error(resp.text):
                cancelled = self.cancel_open_buys_for_symbol(symbol)
                if cancelled:
                    time.sleep(0.5)
                    return self.place_trailing_stop_short(
                        symbol,
                        qty,
                        trail_percent=trail_percent,
                        _retry_on_qty_conflict=False,
                    )
            logger.error(f"Short trailing stop failed: {resp.status_code} {resp.text[:200]}")
            return None

        except Exception as e:
            if _retry_on_qty_conflict and self._is_qty_conflict_error(e):
                cancelled = self.cancel_open_buys_for_symbol(symbol)
                if cancelled:
                    time.sleep(0.5)
                    return self.place_trailing_stop_short(
                        symbol,
                        qty,
                        trail_percent=trail_percent,
                        _retry_on_qty_conflict=False,
                    )
            logger.error(f"Short trailing stop error for {symbol}: {e}")
            return None

    def upgrade_to_trailing_stop(self, symbol: str, qty: int, 
                                  old_stop_order_id: str = None,
                                  trail_percent: float = 3.0) -> Optional[Dict]:
        """
        Upgrade a fixed stop to a trailing stop.
        Cancels the old stop order and places a trailing stop.
        """
        self._ensure_init()
        # Cancel old stop if provided
        if old_stop_order_id:
            try:
                self.cancel_order(old_stop_order_id)
                logger.info(f"Cancelled fixed stop {old_stop_order_id} for {symbol}")
            except Exception:
                pass
        
        return self.place_trailing_stop(symbol, qty, trail_percent)

    def get_latest_price(self, symbol: str) -> float:
        """Get latest trade price for a symbol."""
        self._ensure_init()
        try:
            import requests
            resp = requests.get(
                f"https://data.alpaca.markets/v2/stocks/{symbol}/trades/latest",
                headers=self._rest_headers(),
                timeout=10,
            )
            resp.raise_for_status()
            return float(resp.json().get("trade", {}).get("p", 0))
        except Exception as e:
            logger.debug(f"Latest price failed for {symbol}: {e}")
            return 0

    @staticmethod
    def _normalize_mover_row(row: Dict, source: str = "alpaca_movers") -> Optional[Dict]:
        if not isinstance(row, dict):
            return None
        symbol = str(row.get("symbol", "") or "").upper().strip()
        if not symbol:
            return None
        try:
            price = float(row.get("price", row.get("last_price", 0)) or 0)
        except Exception:
            price = 0.0
        try:
            change_pct = float(
                row.get("change_percent", row.get("percent_change", row.get("change", 0))) or 0
            )
        except Exception:
            change_pct = 0.0
        try:
            volume = int(float(row.get("volume", row.get("day_volume", row.get("v", 0))) or 0))
        except Exception:
            volume = 0
        return {
            "symbol": symbol,
            "price": price,
            "change_pct": change_pct,
            "volume": volume,
            "source": source,
        }

    def get_movers(self, top: int = 20, market_type: str = "stocks") -> List[Dict]:
        """Get top market movers from Alpaca screener."""
        self._ensure_init()
        try:
            resp = requests.get(
                f"https://data.alpaca.markets/v1beta1/screener/{market_type}/movers",
                headers=self._rest_headers(),
                params={"top": max(1, int(top or 20))},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.debug(f"Alpaca movers failed: {resp.status_code} {resp.text[:200]}")
                return []

            payload = resp.json()
            if not isinstance(payload, dict):
                payload = {}
            results: List[Dict] = []
            for bucket_name in ("gainers", "losers"):
                for row in payload.get(bucket_name, []) or []:
                    normalized = self._normalize_mover_row(row)
                    if normalized:
                        normalized["mover_bucket"] = bucket_name
                        results.append(normalized)

            self._capture_payload(f"movers_{market_type}", results)
            return results
        except Exception as e:
            logger.debug(f"Alpaca movers request error: {e}")
            return []

    def _rest_headers(self) -> Dict:
        """REST API headers for direct requests."""
        return {
            'APCA-API-KEY-ID': self.api_key,
            'APCA-API-SECRET-KEY': self.secret_key,
            'Content-Type': 'application/json',
        }

    @property
    def _base_url(self) -> str:
        """Base URL for REST API."""
        if getattr(settings, 'ALPACA_PAPER', True):
            return 'https://paper-api.alpaca.markets'
        return 'https://api.alpaca.markets'

    # ── Market Data ────────────────────────────────────────────────

    def get_price(self, symbol: str) -> float:
        """Get latest trade price for a symbol using Alpaca data API."""
        self._ensure_init()
        try:
            req = StockLatestTradeRequest(symbol_or_symbols=symbol)
            trades = self._data_client.get_stock_latest_trade(req)
            if symbol in trades:
                return float(trades[symbol].price)
            return 0.0
        except Exception as e:
            logger.error(f"Get price failed ({symbol}): {e}")
            return 0.0

    # ── Helpers ────────────────────────────────────────────────────

    def _order_to_dict(self, order) -> Dict:
        """Convert alpaca order object to dict."""
        return {
            "id": str(order.id),
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": str(order.side).split(".")[-1].lower(),
            "type": str(order.type).split(".")[-1].lower(),
            "qty": str(order.qty) if order.qty else None,
            "notional": str(order.notional) if order.notional else None,
            "filled_qty": str(order.filled_qty),
            "filled_avg_price": str(order.filled_avg_price) if order.filled_avg_price else None,
            "status": str(order.status).split(".")[-1].lower(),
            "created_at": str(order.created_at),
            "extended_hours": order.extended_hours,
        }
