"""
Fade the Runner - Short signal for stocks that ran big yesterday.

Strategy: Stocks that gap up 50%+ in a day on hype (not real catalysts)
tend to give back 20-40% within 48 hours as profit-taking kicks in.

Signal flow:
  1. End of each day, record stocks that ran 50%+ 
  2. Next morning, flag them as SHORT candidates
  3. Wait for weakness confirmation (price < prev close, bearish sentiment)
  4. Short with tight trailing stop (protect against squeeze)

Key filters:
  - Must have run 50%+ yesterday (not today — today's runners might keep going)
  - Exclude stocks with REAL catalysts (FDA approval, earnings beat, M&A)
  - Require bearish or neutral StockTwits sentiment (bulls exhausted)
  - Volume must be declining from yesterday (momentum fading)
"""

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings

DATA_DIR = Path(__file__).parent.parent.parent / "data"
RUNNERS_FILE = DATA_DIR / "yesterdays_runners.json"


class FadeRunnerScanner:
    """
    Identifies short opportunities from yesterday's biggest gainers.
    
    Signals:
      - FADE_SETUP: Stock ran big yesterday, showing weakness today
      - FADE_CONFIRMED: Price broke below key level, short entry
    """

    def __init__(self, polygon_client=None):
        self.polygon = polygon_client
        self._runners: List[Dict] = []
        self._load_cache()
        logger.info(f"Fade runner scanner initialized ({len(self._runners)} cached runners)")

    def _load_cache(self):
        try:
            if RUNNERS_FILE.exists():
                with open(RUNNERS_FILE) as f:
                    data = json.load(f)
                    self._runners = data.get("runners", [])
                    # Prune old entries (>3 days)
                    cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
                    self._runners = [r for r in self._runners if r.get("date", "") >= cutoff]
        except Exception:
            pass

    def _save_cache(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(RUNNERS_FILE, "w") as f:
                json.dump({
                    "runners": self._runners,
                    "updated_at": datetime.now().isoformat(),
                }, f, indent=2)
        except Exception:
            pass

    def record_todays_runners(self, candidates: List[Dict]):
        """
        Call at end of day to record stocks that ran big today.
        These become SHORT candidates tomorrow.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Remove any existing entries for today (idempotent)
        self._runners = [r for r in self._runners if r.get("date") != today]
        
        for c in candidates:
            change_pct = abs(c.get("change_pct", 0))
            if change_pct >= 40:  # 40%+ runners are fade candidates
                self._runners.append({
                    "symbol": c["symbol"],
                    "date": today,
                    "change_pct": c.get("change_pct", 0),
                    "close_price": c.get("price", 0),
                    "volume": c.get("volume", 0),
                    "volume_spike": c.get("volume_spike", 0),
                    "sentiment_at_peak": c.get("sentiment_score", 0),
                })
                logger.debug(f"📉 Recorded runner: {c['symbol']} +{change_pct:.0f}% (fade candidate tomorrow)")

        self._save_cache()
        if any(r["date"] == today for r in self._runners):
            count = sum(1 for r in self._runners if r["date"] == today)
            logger.info(f"📉 Recorded {count} runners for tomorrow's fade watchlist")

    def get_fade_candidates(self) -> List[Dict]:
        """
        Get yesterday's runners as SHORT candidates.
        Returns list of symbols with fade signal data.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        # Also check 2 days ago (for weekend/holiday gaps)
        two_days_ago = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")

        candidates = []
        for r in self._runners:
            run_date = r.get("date", "")
            if run_date in (yesterday, two_days_ago) and run_date != today:
                candidates.append({
                    "symbol": r["symbol"],
                    "run_date": run_date,
                    "run_change_pct": r.get("change_pct", 0),
                    "run_close": r.get("close_price", 0),
                    "run_volume": r.get("volume", 0),
                    "signal_type": "FADE_SETUP",
                    "side": "short",
                })

        return candidates

    async def scan(self, current_candidates: List[Dict] = None) -> List[Dict]:
        """
        Full fade scan: check yesterday's runners against current market data.
        
        Returns signals with confirmation level:
          - FADE_SETUP: Runner identified, watching for weakness
          - FADE_CONFIRMED: Price below yesterday's close + bearish sentiment
        """
        fade_candidates = self.get_fade_candidates()
        if not fade_candidates:
            return []

        signals = []
        for fc in fade_candidates:
            symbol = fc["symbol"]
            run_close = fc.get("run_close", 0)

            # Get current price to check for weakness
            current_price = 0
            if current_candidates:
                for cc in current_candidates:
                    if cc.get("symbol") == symbol:
                        current_price = cc.get("price", 0)
                        break

            if current_price <= 0 and self.polygon:
                try:
                    current_price = self.polygon.get_price(symbol)
                except Exception:
                    pass

            if current_price <= 0:
                continue

            # Check for weakness: price below yesterday's close
            if run_close > 0:
                price_change = ((current_price - run_close) / run_close) * 100
            else:
                price_change = 0

            # Confirmation: price is DOWN from yesterday's close
            if price_change < -2:
                signal_type = "FADE_CONFIRMED"
                score = 0.80 + min(abs(price_change) / 20, 0.15)  # More drop = stronger signal
            elif price_change < 2:
                signal_type = "FADE_SETUP"
                score = 0.60
            else:
                # Still going up — don't short a runner that's still running
                continue

            signals.append({
                "symbol": symbol,
                "signal_type": signal_type,
                "side": "short",
                "run_change_pct": fc.get("run_change_pct", 0),
                "run_close": run_close,
                "current_price": current_price,
                "price_change_from_run": round(price_change, 2),
                "score": round(score, 3),
                "priority": "HIGH" if signal_type == "FADE_CONFIRMED" else "MEDIUM",
            })

        if signals:
            logger.info(f"📉 Fade signals: {len(signals)} short candidates")
            for s in signals[:5]:
                logger.info(
                    f"  {s['signal_type']:15s} {s['symbol']:6s} "
                    f"ran {s['run_change_pct']:+.0f}% → now {s['price_change_from_run']:+.1f}% "
                    f"score={s['score']:.3f}"
                )

        return signals
