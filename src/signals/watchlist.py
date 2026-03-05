"""
Dynamic Watchlist - Built overnight, used during trading hours.

Sources (overnight research):
  1. StockTwits trending + per-ticker sentiment
  2. Twitter/X cashtag mentions + sentiment
  3. Perplexity real-time market research
  4. Pharma catalyst calendar
  5. Yesterday's fade candidates
  6. Game film (what worked before)

Rules:
  - Max 25 tickers on watchlist at any time
  - Tickers auto-expire after 3 days if not refreshed
  - Each ticker has a "conviction" score (0-1) 
  - Watchlist is rebuilt every overnight session
  - Intraday: tickers can be added/removed based on live signals
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
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
MAX_WATCHLIST_SIZE = 25
EXPIRY_DAYS = 3


class DynamicWatchlist:
    """
    Self-managing watchlist that gets rebuilt overnight and pruned daily.
    
    Each entry:
      {ticker, conviction, side, reason, source, added_at, last_refreshed, expires_at}
    """

    def __init__(self):
        self._items: Dict[str, Dict] = {}  # ticker -> watchlist entry
        self._load()
        self._prune_expired()
        logger.info(f"Watchlist loaded: {len(self._items)} tickers")

    def _load(self):
        try:
            if WATCHLIST_FILE.exists():
                with open(WATCHLIST_FILE) as f:
                    data = json.load(f)
                    for item in data.get("items", []):
                        self._items[item["ticker"]] = item
        except Exception:
            pass

    def save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(WATCHLIST_FILE, "w") as f:
                json.dump({
                    "items": list(self._items.values()),
                    "updated_at": datetime.now().isoformat(),
                    "count": len(self._items),
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"Watchlist save failed: {e}")

    def _prune_expired(self):
        """Remove tickers that haven't been refreshed in EXPIRY_DAYS."""
        now = time.time()
        expired = [t for t, item in self._items.items() 
                   if now > item.get("expires_at", 0)]
        for t in expired:
            logger.debug(f"Watchlist pruned: {t} (expired)")
            del self._items[t]
        if expired:
            self.save()

    def add(self, ticker: str, conviction: float, side: str = "long",
            reason: str = "", source: str = "", ttl_hours: float = 72) -> bool:
        """
        Add or update a ticker on the watchlist.
        Returns True if added, False if watchlist is full and ticker is too low conviction.
        """
        ticker = ticker.upper().strip()
        if not ticker or not ticker.isalpha() or len(ticker) > 5:
            return False

        now = time.time()
        expires = now + (ttl_hours * 3600)

        # If already on watchlist, update it
        if ticker in self._items:
            existing = self._items[ticker]
            # Keep the higher conviction, merge reasons
            if conviction > existing.get("conviction", 0):
                existing["conviction"] = conviction
            existing["last_refreshed"] = now
            existing["expires_at"] = expires
            if reason and reason not in existing.get("reason", ""):
                existing["reason"] = f"{existing.get('reason', '')} | {reason}".strip(" |")
            if source and source not in existing.get("sources", ""):
                existing["sources"] = f"{existing.get('sources', '')} + {source}".strip(" +")
            self.save()
            return True

        # If watchlist is full, only add if higher conviction than lowest
        if len(self._items) >= MAX_WATCHLIST_SIZE:
            lowest = min(self._items.values(), key=lambda x: x.get("conviction", 0))
            if conviction <= lowest.get("conviction", 0):
                return False
            # Remove lowest to make room
            logger.debug(f"Watchlist: removing {lowest['ticker']} ({lowest['conviction']:.2f}) for {ticker} ({conviction:.2f})")
            del self._items[lowest["ticker"]]

        self._items[ticker] = {
            "ticker": ticker,
            "conviction": round(conviction, 3),
            "side": side,
            "reason": reason,
            "sources": source,
            "added_at": now,
            "last_refreshed": now,
            "expires_at": expires,
        }
        self.save()
        return True

    def remove(self, ticker: str):
        """Remove a ticker from the watchlist."""
        ticker = ticker.upper().strip()
        if ticker in self._items:
            del self._items[ticker]
            self.save()
            logger.info(f"Watchlist removed: {ticker}")

    def get(self, ticker: str) -> Optional[Dict]:
        """Get watchlist entry for a ticker."""
        return self._items.get(ticker.upper())

    def get_all(self, side: str = None) -> List[Dict]:
        """Get all watchlist items, optionally filtered by side (long/short)."""
        items = sorted(self._items.values(), key=lambda x: x.get("conviction", 0), reverse=True)
        if side:
            items = [i for i in items if i.get("side") == side]
        return items

    def get_tickers(self, side: str = None) -> List[str]:
        """Get just the ticker symbols."""
        return [i["ticker"] for i in self.get_all(side)]

    def clear(self):
        """Clear the entire watchlist."""
        self._items.clear()
        self.save()

    def rebuild_overnight(self, stocktwits_trending: List[Dict] = None,
                          twitter_mentions: List[Dict] = None,
                          perplexity_picks: List[Dict] = None,
                          pharma_catalysts: List[Dict] = None,
                          fade_candidates: List[Dict] = None):
        """
        Rebuild the watchlist from overnight research.
        Called once per night. Merges all sources, ranks by conviction.
        """
        logger.info("🌙 Rebuilding overnight watchlist...")
        
        # Don't clear — refresh existing and add new
        # But do prune expired
        self._prune_expired()

        added = 0

        # ── StockTwits trending (conviction based on trending rank + sentiment) ──
        if stocktwits_trending:
            for t in stocktwits_trending[:15]:
                sym = t.get("symbol", "")
                trending_score = t.get("trending_score", 0) / 30.0  # normalize 0-1
                sent_score = t.get("sentiment_score", 0)  # -1 to 1
                # Conviction = trending rank + sentiment direction
                conviction = 0.3 + (trending_score * 0.3) + (max(0, sent_score) * 0.4)
                side = "short" if sent_score < -0.3 else "long"
                if self.add(sym, conviction, side, 
                           f"StockTwits #{int(trending_score*30)} trending",
                           "stocktwits", ttl_hours=24):
                    added += 1

        # ── Perplexity AI picks (highest conviction — real-time web research) ──
        if perplexity_picks:
            bearish_signals = ["declin", "down ", "drop", "fall", "crash", "sell", "bear", "weak", "loss", "miss"]
            for p in perplexity_picks:
                ticker = p.get("ticker", "")
                reason = p.get("reason", "AI pick")
                reason_lower = reason.lower()
                conviction = 0.7 + min(len(reason) / 200, 0.2)  # Longer reason = more researched
                # Detect side from reason text
                is_bearish = sum(1 for w in bearish_signals if w in reason_lower) >= 2
                side = "short" if is_bearish else "long"
                if is_bearish:
                    logger.debug(f"Perplexity pick {ticker} detected as bearish: {reason[:60]}")
                if self.add(ticker, conviction, side, reason, "perplexity", ttl_hours=48):
                    added += 1

        # ── Pharma catalysts (high conviction near PDUFA date) ──
        if pharma_catalysts:
            for cat in pharma_catalysts:
                ticker = cat.get("ticker", "")
                days_until = cat.get("days_until", 99)
                if days_until <= 14:
                    # Closer to catalyst = higher conviction
                    conviction = 0.6 + (0.03 * (14 - days_until))
                    reason = f"FDA {cat.get('catalyst_type','')} {cat.get('drug','')} in {days_until}d"
                    if self.add(ticker, conviction, "long", reason, "pharma", ttl_hours=72):
                        added += 1

        # ── Fade candidates (short) ──
        if fade_candidates:
            for fc in fade_candidates:
                ticker = fc.get("symbol", "")
                run_pct = abs(fc.get("run_change_pct", 0))
                conviction = 0.5 + min(run_pct / 200, 0.3)  # Bigger run = stronger fade signal
                reason = f"Ran {fc.get('run_change_pct',0):+.0f}% yesterday — profit-taking expected"
                if self.add(ticker, conviction, "short", reason, "fade", ttl_hours=36):
                    added += 1

        # ── Twitter mentions ──
        if twitter_mentions:
            for tm in twitter_mentions:
                ticker = tm.get("symbol", "")
                mentions = tm.get("count", 0)
                sentiment = tm.get("sentiment", 0)
                conviction = 0.3 + min(mentions / 100, 0.3) + (max(0, sentiment) * 0.2)
                side = "short" if sentiment < -0.3 else "long"
                if self.add(ticker, conviction, side,
                           f"Twitter: {mentions} mentions, sent={sentiment:+.2f}",
                           "twitter", ttl_hours=24):
                    added += 1

        self.save()
        logger.success(
            f"📋 Watchlist rebuilt: {len(self._items)} tickers "
            f"({added} added/updated, {len(self.get_all('long'))} long, "
            f"{len(self.get_all('short'))} short)"
        )

        # Log top picks
        for item in self.get_all()[:8]:
            logger.info(
                f"  {'🟢' if item['side']=='long' else '🔴'} {item['ticker']:6s} "
                f"conv={item['conviction']:.2f}  {item['reason'][:60]}"
            )

    def validate_with_prices(self, snapshots: Dict[str, Dict]):
        """
        Cross-reference watchlist against real price data.
        Fix mismatched sides, remove contradictions, adjust conviction.
        
        snapshots: {ticker: {"price": float, "change_pct": float, "prev_close": float}}
        """
        removed = []
        flipped = []
        adjusted = []

        for ticker, item in list(self._items.items()):
            snap = snapshots.get(ticker)
            if not snap:
                continue

            change_pct = snap.get("change_pct", 0)
            side = item.get("side", "long")
            conv = item.get("conviction", 0)
            reason = item.get("reason", "").lower()

            # RULE 1: LONG side but stock is significantly down → flip to SHORT or remove
            if side == "long" and change_pct <= -3.0:
                self.remove(ticker)
                removed.append(f"{ticker} ({change_pct:+.1f}%)")
                logger.warning(f"🔍 PRICE CHECK: Removed {ticker} — marked LONG but down {change_pct:.1f}%")
                continue

            # RULE 2: LONG side but stock is moderately down → reduce conviction
            if side == "long" and change_pct <= -1.5:
                item["conviction"] = max(0.1, conv * 0.5)
                adjusted.append(f"{ticker} ({change_pct:+.1f}%)")
                logger.info(f"🔍 PRICE CHECK: Reduced {ticker} conviction — LONG but down {change_pct:.1f}%")

            # RULE 3: SHORT side but stock is ripping up → remove
            if side == "short" and change_pct >= 5.0:
                self.remove(ticker)
                removed.append(f"{ticker} ({change_pct:+.1f}%)")
                logger.warning(f"🔍 PRICE CHECK: Removed {ticker} — marked SHORT but up {change_pct:.1f}%")
                continue

            # RULE 4: Reason contains bearish language but marked LONG
            bearish_words = ["decliner", "decline", "down ", "dropped", "falling", "crash", "sell-off", "selloff"]
            if side == "long" and any(w in reason for w in bearish_words):
                # Check if price confirms bearish
                if change_pct < 0:
                    self.remove(ticker)
                    removed.append(f"{ticker} (bearish reason)")
                    logger.warning(f"🔍 REASON CHECK: Removed {ticker} — LONG with bearish reason + negative price")
                    continue

            # RULE 5: Strong momentum confirmation → boost conviction slightly
            if side == "long" and change_pct >= 5.0 and conv < 0.95:
                item["conviction"] = min(0.98, conv * 1.1)
                logger.debug(f"🔍 PRICE CHECK: Boosted {ticker} — LONG and up {change_pct:.1f}%")

        self.save()

        if removed:
            logger.warning(f"🔍 Price validation removed {len(removed)}: {', '.join(removed)}")
        if adjusted:
            logger.info(f"🔍 Price validation reduced {len(adjusted)}: {', '.join(adjusted)}")

        return {"removed": removed, "flipped": flipped, "adjusted": adjusted}

    def __len__(self):
        return len(self._items)

    def __contains__(self, ticker):
        return ticker.upper() in self._items
