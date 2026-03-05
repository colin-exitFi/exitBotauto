"""
Sentiment Analyzer - Twitter/X + Perplexity news sentiment.
Composite score: twitter 50% + news 50%.
"""

import asyncio
import re
import requests
from typing import Dict, Optional
from loguru import logger

try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer as VADER
    HAS_VADER = True
except ImportError:
    HAS_VADER = False

from config import settings


class SentimentAnalyzer:
    """
    Real-time sentiment from:
      1. Twitter/X: Search $CASHTAG, score with VADER
      2. Perplexity: News sentiment via sonar model
    
    Composite = twitter*0.5 + news*0.5
    Range: -1.0 (bearish) to +1.0 (bullish)
    """

    def __init__(self):
        self._vader = VADER() if HAS_VADER else None
        self._x_bearer = settings.X_BEARER_TOKEN
        self._pplx_key = settings.PERPLEXITY_API_KEY
        self._session = requests.Session()
        self._cache: Dict[str, Dict] = {}  # symbol -> last result
        logger.info("Sentiment analyzer initialized" + (" (VADER loaded)" if HAS_VADER else " (no VADER)"))

    async def analyze(self, symbol: str) -> Dict:
        """Analyze sentiment for a stock symbol."""
        logger.debug(f"Analyzing sentiment for ${symbol}")

        twitter_result = await self._twitter_sentiment(symbol)
        news_result = await self._perplexity_sentiment(symbol)

        twitter_score = twitter_result.get("score", 0.0)
        news_score = news_result.get("score", 0.0)

        # Composite: 50/50
        composite = twitter_score * 0.5 + news_score * 0.5

        result = {
            "symbol": symbol,
            "score": round(composite, 4),
            "twitter_sentiment": round(twitter_score, 4),
            "news_sentiment": round(news_score, 4),
            "twitter_mentions": twitter_result.get("count", 0),
            "engagement": twitter_result.get("engagement", 0),
            "trending": twitter_result.get("count", 0) > 100,
            "news_summary": news_result.get("summary", ""),
        }
        self._cache[symbol] = result
        return result

    def get_cached(self, symbol: str) -> Optional[Dict]:
        return self._cache.get(symbol)

    # ── Twitter/X Sentiment ────────────────────────────────────────

    async def _twitter_sentiment(self, symbol: str) -> Dict:
        """Search Twitter for $CASHTAG mentions, analyze with VADER."""
        if not self._x_bearer:
            logger.debug("No X bearer token, skipping Twitter sentiment")
            return {"score": 0.0, "count": 0, "engagement": 0}

        try:
            tweets = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_tweets, symbol
            )
            if not tweets:
                return {"score": 0.0, "count": 0, "engagement": 0}

            scores = []
            total_engagement = 0
            for tw in tweets:
                text = tw.get("text", "")
                metrics = tw.get("public_metrics", {})
                engagement = (
                    metrics.get("retweet_count", 0)
                    + metrics.get("like_count", 0)
                    + metrics.get("reply_count", 0)
                )
                total_engagement += engagement

                score = self._vader_score(text)
                # Weight by engagement (min 1)
                weight = max(1, engagement)
                scores.append((score, weight))

            if not scores:
                return {"score": 0.0, "count": 0, "engagement": 0}

            total_weight = sum(w for _, w in scores)
            weighted_score = sum(s * w for s, w in scores) / total_weight if total_weight else 0

            return {
                "score": round(weighted_score, 4),
                "count": len(tweets),
                "engagement": total_engagement,
            }
        except Exception as e:
            logger.warning(f"Twitter sentiment error for {symbol}: {e}")
            return {"score": 0.0, "count": 0, "engagement": 0}

    def _fetch_tweets(self, symbol: str, max_results: int = 50) -> list:
        """Fetch recent tweets mentioning $SYMBOL."""
        url = "https://api.twitter.com/2/tweets/search/recent"
        headers = {"Authorization": f"Bearer {self._x_bearer}"}
        params = {
            "query": f"${symbol} lang:en -is:retweet",
            "max_results": min(max_results, 100),
            "tweet.fields": "public_metrics,created_at",
        }
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code == 429:
                logger.warning("Twitter rate limited")
                return []
            if resp.status_code != 200:
                logger.debug(f"Twitter API {resp.status_code}: {resp.text[:200]}")
                return []
            data = resp.json()
            return data.get("data", [])
        except Exception as e:
            logger.warning(f"Twitter fetch error: {e}")
            return []

    def _vader_score(self, text: str) -> float:
        """Score text with VADER. Returns -1 to 1."""
        if not self._vader:
            # Fallback: simple keyword scoring
            return self._keyword_score(text)
        scores = self._vader.polarity_scores(text)
        return scores["compound"]

    def _keyword_score(self, text: str) -> float:
        """Simple keyword fallback if VADER unavailable."""
        text_lower = text.lower()
        bullish = ["moon", "buy", "calls", "bullish", "breakout", "rocket", "🚀", "pump", "long", "going up", "ripping"]
        bearish = ["dump", "sell", "puts", "bearish", "crash", "short", "falling", "overvalued", "scam", "fraud"]
        bull_count = sum(1 for w in bullish if w in text_lower)
        bear_count = sum(1 for w in bearish if w in text_lower)
        total = bull_count + bear_count
        if total == 0:
            return 0.0
        return (bull_count - bear_count) / total

    # ── Perplexity News Sentiment ──────────────────────────────────

    async def _perplexity_sentiment(self, symbol: str) -> Dict:
        """Query Perplexity for real-time news sentiment."""
        if not self._pplx_key:
            logger.debug("No Perplexity API key, skipping news sentiment")
            return {"score": 0.0, "summary": ""}

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._query_perplexity, symbol
            )
            return result
        except Exception as e:
            logger.warning(f"Perplexity sentiment error for {symbol}: {e}")
            return {"score": 0.0, "summary": ""}

    def _query_perplexity(self, symbol: str) -> Dict:
        """Call Perplexity API for news sentiment."""
        url = "https://api.perplexity.ai/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._pplx_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": "sonar",
            "messages": [
                {
                    "role": "system",
                    "content": "You are a financial sentiment analyst. Respond ONLY with a JSON object: {\"score\": <float -1 to 1>, \"summary\": \"<one sentence>\"}. Score: -1=very bearish, 0=neutral, 1=very bullish.",
                },
                {
                    "role": "user",
                    "content": f"What is the current market sentiment for ${symbol} stock based on today's news? Consider earnings, analyst ratings, sector trends, and any breaking news.",
                },
            ],
            "max_tokens": 150,
            "temperature": 0.1,
        }
        try:
            resp = self._session.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code != 200:
                logger.debug(f"Perplexity {resp.status_code}: {resp.text[:200]}")
                return {"score": 0.0, "summary": ""}
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return self._parse_perplexity_response(content)
        except Exception as e:
            logger.warning(f"Perplexity query error: {e}")
            return {"score": 0.0, "summary": ""}

    def _parse_perplexity_response(self, content: str) -> Dict:
        """Parse JSON from Perplexity response."""
        import json
        # Try direct JSON parse
        try:
            # Extract JSON from possibly wrapped response
            match = re.search(r'\{[^}]+\}', content)
            if match:
                parsed = json.loads(match.group())
                score = float(parsed.get("score", 0))
                score = max(-1.0, min(1.0, score))
                return {"score": score, "summary": parsed.get("summary", "")}
        except (json.JSONDecodeError, ValueError):
            pass
        # Fallback: keyword analysis on the response
        return {"score": self._keyword_score(content), "summary": content[:200]}
