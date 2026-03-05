"""
Persistence — Save and restore bot state across restarts.
Stores: positions, trade history, daily P&L, AI layer state, risk state.
All data saved to data/ directory as JSON.
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

from src.data.trade_schema import normalize_trade_record

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

POSITIONS_FILE = DATA_DIR / "positions.json"
TRADES_FILE = DATA_DIR / "trades.json"
PNL_FILE = DATA_DIR / "pnl_state.json"
AI_STATE_FILE = DATA_DIR / "ai_state.json"
BOT_STATE_FILE = DATA_DIR / "bot_state.json"


def save_positions(positions: Dict[str, Dict]):
    """Save active positions to disk."""
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to save positions: {e}")


def load_positions() -> Dict[str, Dict]:
    """Load positions from disk."""
    try:
        if POSITIONS_FILE.exists():
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    logger.info(f"📦 Restored {len(data)} positions from disk")
                    return data
    except Exception as e:
        logger.error(f"Failed to load positions: {e}")
    return {}


def save_trades(trades: List[Dict]):
    """Save trade history to disk (append-friendly)."""
    try:
        existing = load_trades()
        normalized_new = [normalize_trade_record(t) for t in trades]
        # Merge: add new trades that aren't already there (by entry_time + symbol)
        existing_keys = {(t.get("symbol", ""), t.get("entry_time", 0)) for t in existing}
        for t in normalized_new:
            key = (t.get("symbol", ""), t.get("entry_time", 0))
            if key not in existing_keys:
                existing.append(t)
        with open(TRADES_FILE, "w") as f:
            json.dump(existing, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to save trades: {e}")


def load_trades() -> List[Dict]:
    """Load trade history from disk."""
    try:
        if TRADES_FILE.exists():
            with open(TRADES_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception as e:
        logger.error(f"Failed to load trades: {e}")
    return []


def save_pnl_state(pnl_state: Dict):
    """Save P&L tracking state."""
    try:
        with open(PNL_FILE, "w") as f:
            json.dump(pnl_state, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to save P&L state: {e}")


def load_pnl_state() -> Dict:
    """Load P&L tracking state."""
    try:
        if PNL_FILE.exists():
            with open(PNL_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load P&L state: {e}")
    return {
        "total_realized_pnl": 0.0,
        "today_realized_pnl": 0.0,
        "today_date": "",
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "total_fees": 0.0,
        "starting_equity": 1000.0,
        "peak_equity": 1000.0,
    }


def save_ai_state(ai_layers: Dict):
    """Save AI layer outputs so they survive restart."""
    try:
        with open(AI_STATE_FILE, "w") as f:
            json.dump(ai_layers, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to save AI state: {e}")


def load_ai_state() -> Dict:
    """Load AI layer state from disk."""
    try:
        if AI_STATE_FILE.exists():
            with open(AI_STATE_FILE) as f:
                data = json.load(f)
                logger.info("🧠 Restored AI layer state from disk")
                return data
    except Exception as e:
        logger.error(f"Failed to load AI state: {e}")
    return {}


def save_bot_state(state: Dict):
    """Save general bot state (start_time, cycle count, etc)."""
    try:
        with open(BOT_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Failed to save bot state: {e}")


def load_bot_state() -> Dict:
    """Load general bot state."""
    try:
        if BOT_STATE_FILE.exists():
            with open(BOT_STATE_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load bot state: {e}")
    return {}
