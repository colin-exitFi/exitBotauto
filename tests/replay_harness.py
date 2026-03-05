import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import src.main as main_module


@dataclass
class ReplayVerdict:
    symbol: str
    decision: str = "BUY"
    confidence: int = 82
    size_pct: float = 2.0
    trail_pct: float = 3.0
    reasoning: str = "Replay harness verdict"
    provider_used: str = "claude"

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "decision": self.decision,
            "confidence": self.confidence,
            "size_pct": self.size_pct,
            "trail_pct": self.trail_pct,
            "reasoning": self.reasoning,
            "provider_used": self.provider_used,
        }


class ReplayOrchestrator:
    def __init__(self, verdicts: Optional[Dict[str, ReplayVerdict]] = None):
        self._verdicts = verdicts or {}

    async def evaluate(self, symbol: str, price: float, signals_data: Dict):
        return self._verdicts.get(symbol, ReplayVerdict(symbol=symbol))


class ReplaySentimentAnalyzer:
    def get_cached(self, symbol: str):
        return {"score": 0.6}


class ReplayPositionManager:
    def can_enter(self, symbol: str, positions: List[Dict], risk_manager):
        return True


class ReplayRiskManager:
    def __init__(self):
        self.recorded = []

    def can_trade(self):
        return True

    def get_risk_tier(self):
        return {"name": "TEST", "size_pct": 2.0}

    def record_trade(self, trade: Dict):
        self.recorded.append(trade)


class ReplayBroker:
    def __init__(self):
        self.open_symbols = set()
        self.closed_orders: List[Dict] = []

    def mark_open(self, symbol: str):
        self.open_symbols.add(symbol)

    def mark_closed(self, symbol: str, exit_price: float, side: str = "sell"):
        self.open_symbols.discard(symbol)
        ts = datetime.now(timezone.utc).isoformat()
        self.closed_orders.append(
            {
                "symbol": symbol,
                "type": "trailing_stop",
                "side": side,
                "filled_avg_price": str(exit_price),
                "filled_at": ts,
            }
        )

    def get_positions(self):
        return [{"symbol": s} for s in sorted(self.open_symbols)]

    def get_orders(self, status="open"):
        if status == "closed":
            return list(self.closed_orders)
        return []

    def set_positions_from_snapshot(self, positions: List[Dict]):
        syms = set()
        for pos in positions or []:
            symbol = str(pos.get("symbol", "")).upper().strip()
            if symbol:
                syms.add(symbol)
        self.open_symbols = syms

    def add_closed_orders(self, orders: List[Dict]):
        for order in orders or []:
            normalized = {
                "symbol": str(order.get("symbol", "")).upper().strip(),
                "type": order.get("type", "trailing_stop"),
                "side": order.get("side", "sell"),
                "filled_avg_price": str(order.get("filled_avg_price", "")),
                "filled_at": order.get("filled_at") or order.get("updated_at") or order.get("submitted_at") or datetime.now(timezone.utc).isoformat(),
                "updated_at": order.get("updated_at", ""),
                "submitted_at": order.get("submitted_at", ""),
            }
            if normalized["symbol"]:
                self.closed_orders.append(normalized)


class ReplayEntryManager:
    def __init__(
        self,
        broker: ReplayBroker,
        default_quantity: float = 10.0,
        entry_quantities: Optional[Dict[str, float]] = None,
    ):
        self.positions: Dict[str, Dict] = {}
        self.broker = broker
        self.default_quantity = float(default_quantity)
        self.entry_quantities = entry_quantities or {}

    def get_positions(self):
        return list(self.positions.values())

    def remove_position(self, symbol: str):
        self.positions.pop(symbol, None)
        self.broker.open_symbols.discard(symbol)

    async def can_enter(self, symbol: str, check_sentiment: float, positions: List[Dict]):
        return True

    async def enter_position(self, symbol: str, sentiment_data: Dict):
        entry_price = float(sentiment_data.get("decision_price") or sentiment_data.get("signal_price") or 100.0)
        qty = float(self.entry_quantities.get(symbol, self.default_quantity))
        pos = {
            "symbol": symbol,
            "entry_price": entry_price,
            "quantity": qty,
            "entry_time": time.time(),
            "side": "long",
            "has_trailing_stop": True,
            "trail_pct": float(sentiment_data.get("jury_trail_pct", 3.0) or 3.0),
            "_exit_recorded": False,
            "strategy_tag": sentiment_data.get("strategy_tag", "unknown"),
            "signal_sources": sentiment_data.get("signal_sources", ["unknown"]),
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
            "signal_price": sentiment_data.get("signal_price", entry_price),
            "decision_price": sentiment_data.get("decision_price", entry_price),
        }
        self.positions[symbol] = pos
        self.broker.mark_open(symbol)
        return pos

    async def enter_short(self, symbol: str, sentiment_data: Dict):
        entry_price = float(sentiment_data.get("decision_price") or sentiment_data.get("signal_price") or 100.0)
        qty = float(self.entry_quantities.get(symbol, self.default_quantity))
        pos = {
            "symbol": symbol,
            "entry_price": entry_price,
            "quantity": qty,
            "entry_time": time.time(),
            "side": "short",
            "has_trailing_stop": True,
            "trail_pct": float(sentiment_data.get("jury_trail_pct", 3.0) or 3.0),
            "_exit_recorded": False,
            "strategy_tag": sentiment_data.get("strategy_tag", "unknown"),
            "signal_sources": sentiment_data.get("signal_sources", ["unknown"]),
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
            "signal_price": sentiment_data.get("signal_price", entry_price),
            "decision_price": sentiment_data.get("decision_price", entry_price),
        }
        self.positions[symbol] = pos
        self.broker.mark_open(symbol)
        return pos

    def is_market_open(self):
        return True


class BotReplayHarness:
    """
    Deterministic broker-event replay harness for lifecycle tests.
    Event types:
      - scan: {"type":"scan", "candidates":[...]}
      - broker_close: {"type":"broker_close", "symbol":"AAPL", "exit_price":105.0, "side":"sell"}
      - ws_stop_fill: {"type":"ws_stop_fill", "symbol":"AAPL", "fill_price":105.0, "qty":10.0}
      - monitor_positions: {"type":"monitor_positions"}
    """

    def __init__(
        self,
        verdicts: Optional[Dict[str, ReplayVerdict]] = None,
        default_quantity: float = 10.0,
        entry_quantities: Optional[Dict[str, float]] = None,
    ):
        self.broker = ReplayBroker()
        self.entry_manager = ReplayEntryManager(
            self.broker,
            default_quantity=default_quantity,
            entry_quantities=entry_quantities,
        )
        self.risk_manager = ReplayRiskManager()
        self.bot = main_module.TradingBot.__new__(main_module.TradingBot)
        self.bot.alpaca_client = self.broker
        self.bot.entry_manager = self.entry_manager
        self.bot.risk_manager = self.risk_manager
        self.bot.sentiment_analyzer = ReplaySentimentAnalyzer()
        self.bot.position_manager = ReplayPositionManager()
        self.bot.orchestrator = ReplayOrchestrator(verdicts=verdicts)
        self.bot.ai_layers = {}
        self.bot.pnl_state = {}

    async def replay(self, events: List[Dict]):
        for event in events:
            etype = event.get("type")
            if etype == "scan":
                await self.bot._process_candidates(event.get("candidates", []))
            elif etype == "broker_close":
                self.broker.mark_closed(
                    symbol=event["symbol"],
                    exit_price=float(event.get("exit_price", 0.0)),
                    side=event.get("side", "sell"),
                )
            elif etype == "ws_stop_fill":
                self.bot._on_trailing_stop_filled(
                    event["symbol"],
                    float(event.get("fill_price", 0.0)),
                    float(event.get("qty", 0.0)),
                )
            elif etype == "monitor_positions":
                await self.bot._monitor_positions()
            elif etype == "alpaca_rest":
                self.broker.set_positions_from_snapshot(event.get("positions", []))
                self.broker.add_closed_orders(event.get("closed_orders", []))
            elif etype == "alpaca_ws_trade_update":
                self._apply_trade_update_payload(event.get("payload", {}))
            else:
                raise ValueError(f"Unknown replay event type: {etype}")
        return self.bot

    def _apply_trade_update_payload(self, payload: Dict):
        """
        Apply an Alpaca trade-updates payload:
        {"stream":"trade_updates","data":{"event":"fill","order":{...}}}
        """
        stream = payload.get("stream")
        data = payload.get("data", {})
        order = data.get("order", {})
        if stream != "trade_updates":
            return
        if data.get("event") != "fill":
            return
        if order.get("type") != "trailing_stop":
            return

        symbol = str(order.get("symbol", "")).upper().strip()
        if not symbol:
            return
        fill_price = float(order.get("filled_avg_price", 0) or 0)
        filled_qty = float(order.get("filled_qty", order.get("qty", 0)) or 0)
        if fill_price <= 0 or filled_qty <= 0:
            return
        self.bot._on_trailing_stop_filled(symbol, fill_price, filled_qty)

    @classmethod
    def from_transcript(cls, transcript: Dict):
        """
        Build harness from transcript metadata:
          {
            "verdicts": {"AAPL": {...}},
            "default_quantity": 10.0,
            "entry_quantities": {"AAPL": 10.0}
          }
        """
        verdicts = {}
        for symbol, v in (transcript.get("verdicts", {}) or {}).items():
            verdicts[symbol] = ReplayVerdict(
                symbol=symbol,
                decision=v.get("decision", "BUY"),
                confidence=int(v.get("confidence", 82)),
                size_pct=float(v.get("size_pct", 2.0)),
                trail_pct=float(v.get("trail_pct", 3.0)),
                reasoning=v.get("reasoning", "Transcript verdict"),
                provider_used=v.get("provider_used", "claude"),
            )
        return cls(
            verdicts=verdicts,
            default_quantity=float(transcript.get("default_quantity", 10.0)),
            entry_quantities=transcript.get("entry_quantities", {}),
        )

    async def replay_transcript(self, transcript: Dict):
        return await self.replay(transcript.get("events", []))


def load_transcript_fixture(path: str) -> Dict:
    p = Path(path)
    return json.loads(p.read_text())
