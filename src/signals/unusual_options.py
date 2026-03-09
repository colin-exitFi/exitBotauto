"""
Unusual Options Activity (UOA) Scanner — Institutional smart money signal.

When someone drops millions on short-dated calls/puts, they KNOW something.
This is the single strongest leading indicator available to retail traders.

Sources:
  1. Unusual Whales API (free tier: delayed, paid: real-time)
  2. Barchart unusual options (scrape)
  3. CBOE volume data

Signals:
  - Large call sweeps → bullish (someone paying ask for speed)
  - Large put sweeps → bearish
  - Call/put ratio spikes → directional bias
  - Options volume >> open interest → new positions being opened
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

import httpx
from config import settings
from src.signals.unusual_whales import UnusualWhalesClient

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "unusual_options.json"


class UnusualOptionsScanner:
    """Detect unusual options activity as a leading indicator."""

    def __init__(self, uw_client: Optional[UnusualWhalesClient] = None):
        self._cache: List[Dict] = []
        self._last_fetch = 0
        self._fetch_interval = 300  # 5 min
        self.uw = uw_client or UnusualWhalesClient()
        self._load_cache()
        logger.info(f"Unusual options scanner initialized ({len(self._cache)} cached)")

    def _load_cache(self):
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                    self._cache = data.get("options", [])
                    self._last_fetch = data.get("fetched_at", 0)
        except Exception:
            pass

    def _save_cache(self):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump({"options": self._cache, "fetched_at": time.time()}, f, indent=2)
        except Exception:
            pass

    async def scan(self) -> List[Dict]:
        """Scan for unusual options activity. Returns actionable signals."""
        now = time.time()
        if now - self._last_fetch < self._fetch_interval and self._cache:
            return self._cache

        signals = []

        # Source 1: Unusual Whales API
        if self.uw and self.uw.is_configured():
            try:
                screener = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.uw.get_option_contract_screener(
                        limit=60,
                        is_otm=True,
                        min_premium=200_000,
                        min_volume=500,
                        vol_greater_oi=True,
                        max_dte=45,
                        issue_types=["Common Stock"],
                        exclude_ex_div_ticker=True,
                    ),
                )
                signals.extend(self._aggregate_uw_screener(screener))
            except Exception as e:
                logger.debug(f"Unusual Whales screener fetch failed: {e}")

            try:
                flow_alerts = await asyncio.get_event_loop().run_in_executor(
                    None, self.uw.get_flow_alerts, 100_000, None, 100
                )
                signals.extend(self._aggregate_uw_flow(flow_alerts))
            except Exception as e:
                logger.debug(f"Unusual Whales UOA fetch failed: {e}")

        # Source 2: Barchart unusual options volume
        if not signals:
            try:
                barchart = await self._fetch_barchart()
                signals.extend(barchart)
            except Exception as e:
                logger.debug(f"Barchart UOA fetch failed: {e}")

        # Source 3: Perplexity for real-time UOA (uses live web data)
        if not signals:
            try:
                pplx = await self._fetch_via_perplexity()
                signals.extend(pplx)
            except Exception as e:
                logger.debug(f"Perplexity UOA fetch failed: {e}")

        if signals:
            self._cache = self._merge_signals(signals)
            self._last_fetch = now
            self._save_cache()
            logger.info(f"🎯 Unusual options: {len(self._cache)} signals found")

        return self._cache

    async def check_ticker(self, ticker: str) -> Dict:
        """Check options flow for a specific ticker using Alpaca options data."""
        result = {"ticker": ticker, "has_unusual": False, "signals": []}

        if self.uw and self.uw.is_configured():
            try:
                flow, recent_flow, volume, iv = await asyncio.gather(
                    asyncio.get_event_loop().run_in_executor(None, self.uw.summarize_flow_for_symbol, ticker),
                    asyncio.get_event_loop().run_in_executor(None, self.uw.summarize_recent_flow_for_symbol, ticker, 100_000),
                    asyncio.get_event_loop().run_in_executor(None, self.uw.summarize_options_volume, ticker, 1),
                    asyncio.get_event_loop().run_in_executor(None, self.uw.summarize_interpolated_iv, ticker, 30, None),
                )
                result.update(flow)
                result["recent_flow"] = recent_flow
                result["options_volume"] = volume
                result["iv_summary"] = iv
                if recent_flow.get("bias") in {"bullish", "bearish"}:
                    result["bias"] = recent_flow.get("bias")
                    result["has_unusual"] = True
                if volume.get("bias") in {"bullish", "bearish"}:
                    result["options_volume_bias"] = volume.get("bias")
                return result
            except Exception as e:
                logger.debug(f"UW ticker flow check failed for {ticker}: {e}")

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Alpaca options snapshot
                resp = await client.get(
                    f"https://data.alpaca.markets/v1beta1/options/snapshots/{ticker}",
                    headers={
                        "APCA-API-KEY-ID": settings.ALPACA_API_KEY,
                        "APCA-API-SECRET-KEY": settings.ALPACA_SECRET_KEY,
                    },
                    params={"feed": "indicative", "limit": 20},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    snapshots = data.get("snapshots", {})
                    total_call_vol = 0
                    total_put_vol = 0
                    total_call_oi = 0
                    total_put_oi = 0

                    for contract_id, snap in snapshots.items():
                        trade = snap.get("latestTrade", {})
                        vol = trade.get("s", 0)  # size
                        greeks = snap.get("greeks", {})

                        if "C" in contract_id.split(ticker)[-1][:10]:
                            total_call_vol += vol
                        else:
                            total_put_vol += vol

                    if total_call_vol + total_put_vol > 0:
                        ratio = total_call_vol / max(total_put_vol, 1)
                        result["call_volume"] = total_call_vol
                        result["put_volume"] = total_put_vol
                        result["call_put_ratio"] = round(ratio, 2)
                        result["bias"] = "bullish" if ratio > 1.5 else ("bearish" if ratio < 0.67 else "neutral")
                        if ratio > 2.0 or ratio < 0.5:
                            result["has_unusual"] = True
                            result["signals"].append({
                                "type": "call_put_ratio",
                                "ratio": round(ratio, 2),
                                "bias": result["bias"],
                            })
        except Exception as e:
            logger.debug(f"Options check failed for {ticker}: {e}")

        return result

    @staticmethod
    def _aggregate_uw_flow(flow_alerts: List[Dict]) -> List[Dict]:
        by_ticker: Dict[str, Dict] = {}
        for alert in flow_alerts or []:
            ticker = str(alert.get("ticker", "")).upper()
            if not ticker:
                continue
            bucket = by_ticker.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "bullish_premium": 0.0,
                    "bearish_premium": 0.0,
                    "volume": 0,
                    "open_interest": 0,
                    "count": 0,
                    "source": "unusual_whales",
                },
            )
            sentiment = alert.get("sentiment")
            premium = float(alert.get("premium", 0.0) or 0.0)
            if sentiment == "bullish":
                bucket["bullish_premium"] += premium
            elif sentiment == "bearish":
                bucket["bearish_premium"] += premium
            bucket["volume"] += int(alert.get("volume", 0) or 0)
            bucket["open_interest"] += int(alert.get("open_interest", 0) or 0)
            bucket["count"] += 1

        signals = []
        for ticker, bucket in by_ticker.items():
            bullish = bucket["bullish_premium"]
            bearish = bucket["bearish_premium"]
            if bullish <= 0 and bearish <= 0:
                continue
            bias = "bullish" if bullish >= bearish else "bearish"
            conviction = 0.55
            if max(bullish, bearish) >= 250_000:
                conviction = 0.7
            if max(bullish, bearish) >= 1_000_000:
                conviction = 0.85
            signals.append(
                {
                    "ticker": ticker,
                    "type": "call" if bias == "bullish" else "put",
                    "bias": bias,
                    "reason": (
                        f"Whale flow {bias}: ${max(bullish, bearish):,.0f} premium across {bucket['count']} alerts"
                    ),
                    "source": "unusual_whales",
                    "conviction": conviction,
                    "premium": round(max(bullish, bearish), 2),
                    "volume": bucket["volume"],
                    "open_interest": bucket["open_interest"],
                }
            )
        signals.sort(key=lambda item: item.get("premium", 0), reverse=True)
        return signals

    @staticmethod
    def _aggregate_uw_screener(records: List[Dict]) -> List[Dict]:
        summaries = UnusualWhalesClient.summarize_option_screener(records)
        signals = []
        for summary in summaries:
            contracts = int(summary.get("contracts", 0) or 0)
            total_premium = float(summary.get("total_premium", 0.0) or 0.0)
            if contracts < 2 and total_premium < 500_000:
                continue
            bias = str(summary.get("bias") or "neutral").lower()
            if bias not in {"bullish", "bearish"}:
                continue
            conviction = 0.65
            if total_premium >= 1_000_000 or float(summary.get("avg_ask_side_pct", 0.0) or 0.0) >= 0.70:
                conviction = 0.8
            signals.append(
                {
                    "ticker": summary.get("ticker"),
                    "type": "call" if bias == "bullish" else "put",
                    "bias": bias,
                    "reason": (
                        f"Hottest contracts {bias}: ${total_premium:,.0f} premium across {contracts} contracts "
                        f"(ask-side {float(summary.get('avg_ask_side_pct', 0.0) or 0.0):.0%}, "
                        f"vol/OI {float(summary.get('avg_vol_to_oi', 0.0) or 0.0):.2f})"
                    ),
                    "source": "unusual_whales_screener",
                    "conviction": conviction,
                    "premium": round(total_premium, 2),
                    "volume": contracts,
                    "open_interest": 0,
                }
            )
        return signals

    @staticmethod
    def _merge_signals(signals: List[Dict]) -> List[Dict]:
        merged: Dict[str, Dict] = {}
        for signal in signals or []:
            ticker = str(signal.get("ticker", "")).upper()
            if not ticker:
                continue
            current = merged.get(ticker)
            if not current:
                merged[ticker] = dict(signal)
                continue
            if float(signal.get("premium", 0.0) or 0.0) > float(current.get("premium", 0.0) or 0.0):
                current["premium"] = signal.get("premium", current.get("premium"))
                current["reason"] = signal.get("reason", current.get("reason"))
                current["type"] = signal.get("type", current.get("type"))
                current["bias"] = signal.get("bias", current.get("bias"))
                current["conviction"] = signal.get("conviction", current.get("conviction"))
            current["source"] = f"{current.get('source', '')}+{signal.get('source', '')}".strip("+")
            current["volume"] = int(current.get("volume", 0) or 0) + int(signal.get("volume", 0) or 0)
        merged_signals = list(merged.values())
        merged_signals.sort(key=lambda item: float(item.get("premium", 0.0) or 0.0), reverse=True)
        return merged_signals

    async def _fetch_barchart(self) -> List[Dict]:
        """Scrape Barchart for unusual options volume."""
        signals = []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://www.barchart.com/options/unusual-activity/stocks",
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                        "Accept": "text/html",
                    },
                )
                if resp.status_code == 200:
                    import re
                    # Extract ticker symbols from the page
                    tickers = re.findall(r'class="symbol"[^>]*>([A-Z]{1,5})</a>', resp.text)
                    types = re.findall(r'<td[^>]*>(Call|Put)</td>', resp.text)
                    volumes = re.findall(r'data-value="(\d+)"', resp.text)

                    for i, ticker in enumerate(tickers[:20]):
                        opt_type = types[i] if i < len(types) else "unknown"
                        signal = {
                            "ticker": ticker,
                            "type": opt_type.lower(),
                            "source": "barchart",
                            "bias": "bullish" if opt_type == "Call" else "bearish",
                            "conviction": 0.6,
                            "reason": f"Unusual {opt_type} volume on Barchart",
                        }
                        signals.append(signal)
        except Exception as e:
            logger.debug(f"Barchart scrape failed: {e}")
        return signals

    async def _fetch_via_perplexity(self) -> List[Dict]:
        """Use Perplexity to find unusual options activity from live web."""
        pplx_key = getattr(settings, 'PERPLEXITY_API_KEY', None)
        if not pplx_key:
            return []

        signals = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {pplx_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "sonar-pro",
                        "max_tokens": 800,
                        "messages": [{"role": "user", "content":
                            "What are the top 10 stocks with unusual options activity today? "
                            "Include the ticker, whether it's calls or puts, and the bias (bullish/bearish). "
                            "Format each as: TICKER|call_or_put|bullish_or_bearish|brief_reason "
                            "One per line."}],
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                for line in text.strip().split("\n"):
                    parts = line.strip().split("|")
                    if len(parts) >= 3:
                        ticker = parts[0].strip().upper()
                        if ticker.isalpha() and len(ticker) <= 5:
                            signals.append({
                                "ticker": ticker,
                                "type": parts[1].strip().lower(),
                                "bias": parts[2].strip().lower(),
                                "reason": parts[3].strip() if len(parts) > 3 else "Unusual options flow",
                                "source": "perplexity",
                                "conviction": 0.5,
                            })
        except Exception as e:
            logger.debug(f"Perplexity UOA failed: {e}")
        return signals

    def get_bullish_tickers(self) -> List[str]:
        """Get tickers with bullish unusual options flow."""
        return [s["ticker"] for s in self._cache if s.get("bias") == "bullish"]

    def get_bearish_tickers(self) -> List[str]:
        """Get tickers with bearish unusual options flow (short candidates)."""
        return [s["ticker"] for s in self._cache if s.get("bias") == "bearish"]
