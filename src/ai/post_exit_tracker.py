"""
Post-Exit Price Tracker

Tracks where price goes AFTER we exit a trade.
Answers the question: "Did we get out too early?"

Runs every 5 minutes, checks recent exits, fills:
- post_exit_1h_price: price 1 hour after exit
- post_exit_4h_price: price 4 hours after exit  
- post_exit_1d_price: price 1 day after exit
- left_on_table_pct: how much more the stock moved in our direction after exit
"""

import asyncio
import time
import json
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger


DATA_DIR = Path(__file__).parent.parent.parent / "data"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.json"
POST_EXIT_FILE = DATA_DIR / "post_exit_tracking.json"


def _load_trades() -> List[Dict]:
    try:
        if TRADE_HISTORY_FILE.exists():
            return json.loads(TRADE_HISTORY_FILE.read_text())
    except Exception:
        pass
    return []


def _save_trades(trades: List[Dict]):
    try:
        TRADE_HISTORY_FILE.write_text(json.dumps(trades, indent=1, default=str))
    except Exception as e:
        logger.debug(f"Post-exit save failed: {e}")


def _load_tracking() -> Dict:
    try:
        if POST_EXIT_FILE.exists():
            return json.loads(POST_EXIT_FILE.read_text())
    except Exception:
        pass
    return {"pending": [], "completed": [], "summary": {}}


def _save_tracking(data: Dict):
    try:
        POST_EXIT_FILE.write_text(json.dumps(data, indent=2, default=str))
    except Exception as e:
        logger.debug(f"Post-exit tracking save failed: {e}")


async def check_post_exit_prices(get_price_fn) -> Dict:
    """
    Check prices for recently exited trades.
    get_price_fn: async function that takes symbol and returns current price.
    Returns summary of what was found.
    """
    trades = _load_trades()
    tracking = _load_tracking()
    now = time.time()
    updated = 0
    left_on_table_total = 0.0
    dodged_bullets_total = 0.0

    for trade in trades:
        exit_time = float(trade.get("exit_time", 0) or 0)
        if exit_time <= 0:
            continue

        symbol = trade.get("symbol", "")
        if not symbol:
            continue

        side = str(trade.get("side", "long") or "long").lower()
        exit_price = float(trade.get("exit_price", 0) or 0)
        if exit_price <= 0:
            continue

        hours_since_exit = (now - exit_time) / 3600

        needs_1h = trade.get("post_exit_1h_price") is None and 1.0 <= hours_since_exit <= 48
        needs_4h = trade.get("post_exit_4h_price") is None and 4.0 <= hours_since_exit <= 48
        needs_1d = trade.get("post_exit_1d_price") is None and 24.0 <= hours_since_exit <= 168

        if not (needs_1h or needs_4h or needs_1d):
            continue

        try:
            current_price = await get_price_fn(symbol)
            if not current_price or current_price <= 0:
                continue
        except Exception:
            continue

        if needs_1h and 1.0 <= hours_since_exit < 4.0:
            trade["post_exit_1h_price"] = current_price
            updated += 1

        if needs_4h and 4.0 <= hours_since_exit < 24.0:
            trade["post_exit_4h_price"] = current_price
            updated += 1

        if needs_1d and 24.0 <= hours_since_exit < 168.0:
            trade["post_exit_1d_price"] = current_price
            updated += 1

        # Calculate left on table
        best_post_price = None
        for field in ["post_exit_1d_price", "post_exit_4h_price", "post_exit_1h_price"]:
            p = trade.get(field)
            if p and float(p) > 0:
                best_post_price = float(p)
                break

        if best_post_price and exit_price > 0:
            if "short" in side or "cover" in side:
                continued_move_pct = (exit_price - best_post_price) / exit_price * 100
            else:
                continued_move_pct = (best_post_price - exit_price) / exit_price * 100

            trade["post_exit_continued_move_pct"] = round(continued_move_pct, 2)

            if continued_move_pct > 0:
                left_on_table_total += continued_move_pct
            else:
                dodged_bullets_total += abs(continued_move_pct)

    if updated > 0:
        _save_trades(trades)
        logger.info(f"📊 Post-exit tracker: updated {updated} price checks")

    # Build summary
    trades_with_post = [t for t in trades if t.get("post_exit_continued_move_pct") is not None]
    if trades_with_post:
        left_on_table = [t for t in trades_with_post if float(t.get("post_exit_continued_move_pct", 0)) > 0.5]
        dodged = [t for t in trades_with_post if float(t.get("post_exit_continued_move_pct", 0)) < -0.5]
        neutral = [t for t in trades_with_post if abs(float(t.get("post_exit_continued_move_pct", 0))) <= 0.5]

        avg_left = sum(float(t.get("post_exit_continued_move_pct", 0)) for t in left_on_table) / max(1, len(left_on_table))
        avg_dodged = sum(abs(float(t.get("post_exit_continued_move_pct", 0))) for t in dodged) / max(1, len(dodged))

        summary = {
            "total_tracked": len(trades_with_post),
            "left_on_table_count": len(left_on_table),
            "left_on_table_avg_pct": round(avg_left, 2),
            "dodged_bullets_count": len(dodged),
            "dodged_bullets_avg_pct": round(avg_dodged, 2),
            "neutral_count": len(neutral),
            "worst_left_on_table": sorted(left_on_table, key=lambda t: -float(t.get("post_exit_continued_move_pct", 0)))[:5],
            "best_dodged": sorted(dodged, key=lambda t: float(t.get("post_exit_continued_move_pct", 0)))[:5],
            "updated_at": time.time(),
        }

        tracking["summary"] = summary
        _save_tracking(tracking)

        if left_on_table:
            biggest = left_on_table[0] if left_on_table else None
            if biggest:
                logger.info(
                    f"📊 Post-exit: {len(left_on_table)} exits left money on table (avg +{avg_left:.1f}% more), "
                    f"{len(dodged)} dodged bullets (avg {avg_dodged:.1f}% saved). "
                    f"Biggest miss: {biggest.get('symbol')} +{float(biggest.get('post_exit_continued_move_pct', 0)):.1f}% after exit"
                )

    return {"updated": updated, "tracked": len(trades_with_post)}
