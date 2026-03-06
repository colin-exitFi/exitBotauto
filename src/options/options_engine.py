"""
Options Engine — Converts jury BUY/SHORT verdicts into options trades.

Strategy:
- BUY verdict → Buy near-the-money CALL, 7-14 days to expiry
- SHORT verdict → Buy near-the-money PUT, 7-14 days to expiry
- Max risk per trade = premium paid (can't lose more than cost)
- Exit: sell option when underlying trailing stop triggers, or at 50% profit, or at 50% loss

Contract selection:
1. Find options chain for the underlying
2. Pick expiry 7-14 days out (nearest Friday)
3. Pick strike nearest to current price (ATM) or 1 strike OTM for leverage
4. Verify: tradable, reasonable spread, sufficient open interest
"""

import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from loguru import logger

import httpx


class OptionsEngine:
    """Handles options contract selection and order placement via Alpaca."""

    def __init__(self, api_key: str, secret_key: str, base_url: str = "https://paper-api.alpaca.markets"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        # Track options positions separately
        self.positions: Dict[str, Dict] = {}
        logger.info("Options engine initialized")

    def _get_target_expiry_range(self) -> Tuple[str, str]:
        """Get target expiry range: 7-14 days out."""
        today = datetime.now()
        min_expiry = today + timedelta(days=7)
        max_expiry = today + timedelta(days=21)  # wider range to find liquid contracts
        return min_expiry.strftime("%Y-%m-%d"), max_expiry.strftime("%Y-%m-%d")

    def find_contract(
        self,
        symbol: str,
        price: float,
        direction: str = "BUY",  # BUY = call, SHORT = put
    ) -> Optional[Dict]:
        """
        Find the best options contract for a given trade signal.
        
        Returns contract dict or None if no suitable contract found.
        """
        option_type = "call" if direction == "BUY" else "put"
        min_expiry, max_expiry = self._get_target_expiry_range()

        try:
            # Fetch options chain
            resp = httpx.get(
                f"{self.base_url}/v2/options/contracts",
                headers=self._headers,
                params={
                    "underlying_symbols": symbol,
                    "type": option_type,
                    "expiration_date_gte": min_expiry,
                    "expiration_date_lte": max_expiry,
                    "limit": 100,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(f"Options chain lookup failed for {symbol}: {resp.status_code} {resp.text[:200]}")
                return None

            data = resp.json()
            contracts = data.get("option_contracts", [])
            if not contracts:
                logger.info(f"No {option_type} contracts found for {symbol} (expiry {min_expiry} to {max_expiry})")
                return None

            # Filter to tradable contracts only
            tradable = [c for c in contracts if c.get("tradable")]
            if not tradable:
                logger.info(f"No tradable {option_type} contracts for {symbol}")
                return None

            # Find nearest ATM or slightly OTM strike
            best = None
            best_score = float("inf")
            for c in tradable:
                strike = float(c.get("strike_price", 0))
                if strike <= 0:
                    continue

                if direction == "BUY":
                    # For calls: ATM or slightly OTM (strike >= price)
                    # Prefer strike closest to price but not too deep ITM
                    distance = abs(strike - price) / price
                    # Slight preference for OTM (cheaper premium, more leverage)
                    if strike >= price:
                        score = distance
                    else:
                        score = distance + 0.01  # slight penalty for ITM
                else:
                    # For puts: ATM or slightly OTM (strike <= price)
                    distance = abs(strike - price) / price
                    if strike <= price:
                        score = distance
                    else:
                        score = distance + 0.01

                if score < best_score:
                    best_score = score
                    best = c

            if best:
                logger.info(
                    f"📋 Options contract: {best['symbol']} "
                    f"({best['name']}) strike=${best['strike_price']} "
                    f"exp={best['expiration_date']}"
                )
            return best

        except Exception as e:
            logger.error(f"Options chain error for {symbol}: {e}")
            return None

    def get_option_quote(self, contract_symbol: str) -> Optional[Dict]:
        """Get current quote for an options contract."""
        try:
            resp = httpx.get(
                f"https://data.alpaca.markets/v1beta1/options/quotes/latest",
                headers=self._headers,
                params={"symbols": contract_symbol, "feed": "indicative"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                quote = data.get("quotes", {}).get(contract_symbol)
                return quote
            return None
        except Exception as e:
            logger.error(f"Options quote error: {e}")
            return None

    def place_option_order(
        self,
        contract_symbol: str,
        qty: int = 1,
        side: str = "buy",
        order_type: str = "market",
        limit_price: float = None,
    ) -> Optional[Dict]:
        """Place an options order via Alpaca."""
        order_data = {
            "symbol": contract_symbol,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": "day",
        }
        if order_type == "limit" and limit_price:
            order_data["limit_price"] = str(round(limit_price, 2))

        try:
            resp = httpx.post(
                f"{self.base_url}/v2/orders",
                headers=self._headers,
                json=order_data,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                order = resp.json()
                logger.success(
                    f"🎯 Options order: {side.upper()} {qty}x {contract_symbol} "
                    f"→ {order.get('id', '')[:8]}"
                )
                return order
            else:
                logger.error(f"Options order failed: {resp.status_code} {resp.text[:200]}")
                return None
        except Exception as e:
            logger.error(f"Options order error: {e}")
            return None

    async def execute_option_trade(
        self,
        symbol: str,
        price: float,
        direction: str,
        budget: float,
        sentiment_data: Dict = None,
    ) -> Optional[Dict]:
        """
        Full options trade flow:
        1. Find best contract
        2. Get quote / estimate cost
        3. Calculate quantity (max contracts within budget)
        4. Place order
        5. Track position
        """
        sentiment_data = sentiment_data or {}
        
        # Find contract
        contract = self.find_contract(symbol, price, direction)
        if not contract:
            logger.info(f"No suitable options contract for {symbol} {direction}")
            return None

        contract_symbol = contract["symbol"]
        strike = float(contract.get("strike_price", 0))
        expiry = contract.get("expiration_date", "")

        # Get quote for pricing
        quote = self.get_option_quote(contract_symbol)
        
        # Estimate premium: use quote if available, else rough estimate
        if quote and quote.get("ap", 0) > 0:
            ask_price = float(quote["ap"])
            bid_price = float(quote.get("bp", 0))
            # Check spread isn't too wide (> 20% of mid = illiquid)
            mid = (ask_price + bid_price) / 2 if bid_price > 0 else ask_price
            if bid_price > 0 and (ask_price - bid_price) / mid > 0.20:
                logger.warning(f"Options spread too wide for {contract_symbol}: bid={bid_price} ask={ask_price}")
                return None
            premium_per_contract = ask_price * 100  # each contract = 100 shares
        else:
            # Rough estimate: ATM option ≈ 3-5% of stock price
            estimated_premium = price * 0.04
            premium_per_contract = estimated_premium * 100
            ask_price = estimated_premium

        if premium_per_contract <= 0:
            logger.warning(f"Cannot estimate premium for {contract_symbol}")
            return None

        # Calculate quantity within budget
        max_contracts = int(budget / premium_per_contract)
        if max_contracts < 1:
            logger.info(f"Budget ${budget:.0f} insufficient for {contract_symbol} (premium ~${premium_per_contract:.0f}/contract)")
            return None

        qty = min(max_contracts, 5)  # Cap at 5 contracts per trade

        # Place the order
        order = self.place_option_order(
            contract_symbol=contract_symbol,
            qty=qty,
            side="buy",
            order_type="limit",
            limit_price=round(ask_price * 1.02, 2),  # 2% above ask for fill
        )

        if order:
            position = {
                "underlying": symbol,
                "contract_symbol": contract_symbol,
                "direction": direction,
                "option_type": "call" if direction == "BUY" else "put",
                "strike": strike,
                "expiry": expiry,
                "qty": qty,
                "entry_premium": ask_price,
                "total_cost": qty * ask_price * 100,
                "entry_time": time.time(),
                "order_id": order.get("id"),
                "status": "pending",
                "sentiment_data": {
                    "confidence": sentiment_data.get("consensus_confidence", 0),
                    "strategy_tag": sentiment_data.get("strategy_tag", ""),
                },
            }
            self.positions[contract_symbol] = position
            logger.success(
                f"🎯 OPTIONS ENTRY: {qty}x {contract_symbol} "
                f"(${strike} {position['option_type']}, exp {expiry}) "
                f"@ ~${ask_price:.2f}/contract = ${position['total_cost']:.0f} total"
            )
            return position

        return None

    def get_options_positions(self) -> List[Dict]:
        """Get all tracked options positions."""
        return list(self.positions.values())

    def close_option_position(self, contract_symbol: str, qty: int = None) -> Optional[Dict]:
        """Sell an options position."""
        pos = self.positions.get(contract_symbol)
        if not pos:
            return None
        
        sell_qty = qty or pos.get("qty", 1)
        order = self.place_option_order(
            contract_symbol=contract_symbol,
            qty=sell_qty,
            side="sell",
            order_type="market",
        )
        if order:
            pos["status"] = "closing"
            pos["close_order_id"] = order.get("id")
        return order
