"""
Twitter/X Sentiment - Cashtag mentions, engagement, and VADER sentiment.
Uses Twitter API v2 recent search with Bearer Token auth.
"""

import time
import requests
from typing import Dict, List
from loguru import logger
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings

BASE_URL = "https://api.twitter.com/2"
TIMEOUT = 10


class TwitterSentimentClient:
    """Fetch cashtag sentiment and engagement from Twitter/X API v2."""

    def __init__(self):
        self._bearer = settings.X_BEARER_TOKEN
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._bearer}",
            "User-Agent": "Velox/1.0",
        })
        self._vader = SentimentIntensityAnalyzer()
        self._cache: Dict[str, Dict] = {}
        self._cache_ttl = 300  # 5 min

    def get_cashtag_sentiment(self, symbol: str) -> Dict:
        """
        Search for $SYMBOL mentions in last hour, analyze sentiment.
        Returns {tweet_count, avg_sentiment, bullish_pct, engagement_score}.
        """
        # Check cache
        cached = self._cache.get(symbol)
        if cached and time.time() - cached.get("_ts", 0) < self._cache_ttl:
            return cached

        if not self._bearer:
            return {"tweet_count": 0, "avg_sentiment": 0, "bullish_pct": 0.5, "engagement_score": 0}

        try:
            resp = self._session.get(
                f"{BASE_URL}/tweets/search/recent",
                params={
                    "query": f"${symbol} lang:en -is:retweet",
                    "max_results": 100,
                    "tweet.fields": "public_metrics,created_at,text",
                },
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            tweets = data.get("data", [])

            if not tweets:
                result = {"tweet_count": 0, "avg_sentiment": 0, "bullish_pct": 0.5, "engagement_score": 0}
                self._cache[symbol] = {**result, "_ts": time.time()}
                return result

            sentiments = []
            total_engagement = 0
            bullish_count = 0

            for tweet in tweets:
                text = tweet.get("text", "")
                scores = self._vader.polarity_scores(text)
                compound = scores["compound"]
                sentiments.append(compound)

                if compound > 0.05:
                    bullish_count += 1

                metrics = tweet.get("public_metrics", {})
                engagement = (
                    metrics.get("like_count", 0) * 1
                    + metrics.get("retweet_count", 0) * 3
                    + metrics.get("reply_count", 0) * 2
                    + metrics.get("quote_count", 0) * 4
                )
                total_engagement += engagement

            avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0
            bullish_pct = bullish_count / len(tweets) if tweets else 0.5
            # Normalize engagement: log scale, cap at 1.0
            import math
            engagement_score = min(1.0, math.log1p(total_engagement) / 10.0)

            result = {
                "tweet_count": len(tweets),
                "avg_sentiment": round(avg_sentiment, 4),
                "bullish_pct": round(bullish_pct, 4),
                "engagement_score": round(engagement_score, 4),
            }
            self._cache[symbol] = {**result, "_ts": time.time()}
            return result

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                logger.warning("Twitter rate limited, using cache")
            else:
                logger.warning(f"Twitter sentiment error for {symbol}: {e}")
            return self._cache.get(symbol, {"tweet_count": 0, "avg_sentiment": 0, "bullish_pct": 0.5, "engagement_score": 0})
        except Exception as e:
            logger.warning(f"Twitter sentiment failed for {symbol}: {e}")
            return {"tweet_count": 0, "avg_sentiment": 0, "bullish_pct": 0.5, "engagement_score": 0}

    def get_trending_cashtags(self, symbols: List[str]) -> List[Dict]:
        """
        Scan a list of symbols on Twitter for volume/sentiment.
        Returns sorted list of {symbol, tweet_count, avg_sentiment, engagement_score}.
        """
        results = []
        for symbol in symbols[:20]:  # Cap API calls
            sent = self.get_cashtag_sentiment(symbol)
            if sent.get("tweet_count", 0) > 0:
                results.append({"symbol": symbol, **sent})

        results.sort(key=lambda x: x.get("engagement_score", 0), reverse=True)
        return results
