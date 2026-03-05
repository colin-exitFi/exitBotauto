"""
StockTwits Client - Trending tickers and sentiment data.
No authentication required for public endpoints.
"""

import requests
from typing import Dict, List
from loguru import logger

BASE_URL = "https://api.stocktwits.com/api/2"
TIMEOUT = 10


class StockTwitsClient:
    """Fetch trending symbols and per-symbol sentiment from StockTwits."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "exitBotauto/1.0",
            "Accept": "application/json",
        })

    def get_trending(self) -> List[Dict]:
        """
        Get trending symbols from StockTwits.
        Returns list of {symbol, trending_score, title, sector}.
        """
        try:
            resp = self._session.get(
                f"{BASE_URL}/trending/symbols.json",
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            symbols = data.get("symbols", [])
            results = []
            for i, sym in enumerate(symbols):
                results.append({
                    "symbol": sym.get("symbol", ""),
                    "trending_score": len(symbols) - i,  # higher rank = higher score
                    "title": sym.get("title", sym.get("symbol", "")),
                    "sector": sym.get("sector", ""),
                })
            logger.debug(f"StockTwits trending: {len(results)} symbols")
            return results
        except requests.exceptions.HTTPError as e:
            logger.warning(f"StockTwits trending HTTP error: {e}")
            return []
        except Exception as e:
            logger.warning(f"StockTwits trending failed: {e}")
            return []

    def get_sentiment(self, symbol: str) -> Dict:
        """
        Get sentiment for a symbol from StockTwits.
        Tries the streams endpoint and parses message sentiment.
        Returns {bullish, bearish, score} where score is -1 to 1.
        """
        try:
            resp = self._session.get(
                f"{BASE_URL}/streams/symbol/{symbol}.json",
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            messages = data.get("messages", [])
            bullish = 0
            bearish = 0
            for msg in messages:
                sentiment = msg.get("entities", {}).get("sentiment", {})
                if sentiment:
                    basic = sentiment.get("basic", "")
                    if basic == "Bullish":
                        bullish += 1
                    elif basic == "Bearish":
                        bearish += 1
            total = bullish + bearish
            if total == 0:
                score = 0.0
            else:
                score = (bullish - bearish) / total  # range -1 to 1
            return {
                "bullish": bullish,
                "bearish": bearish,
                "score": round(score, 4),
                "total_messages": len(messages),
            }
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.debug(f"StockTwits sentiment not found for {symbol}")
            else:
                logger.warning(f"StockTwits sentiment HTTP error for {symbol}: {e}")
            return {"bullish": 0, "bearish": 0, "score": 0.0, "total_messages": 0}
        except Exception as e:
            logger.warning(f"StockTwits sentiment failed for {symbol}: {e}")
            return {"bullish": 0, "bearish": 0, "score": 0.0, "total_messages": 0}
