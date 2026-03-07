"""
Tier-1 copy-trader monitor.

V2 prefers X filtered-stream delivery with recent-search polling as fallback.
Entry and exit signals are still parsed locally with regex to keep latency and
cost low.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from loguru import logger

from config import settings


BASE_URL = "https://api.twitter.com/2"
TICKER_RE = re.compile(r"\$([A-Z]{1,5})\b")
BUY_HINTS = (" long ", " bought ", " buy ", " starter ", " adding ", " added ", " entry ", " swing long ")
SHORT_HINTS = (" short ", " shorting ", " started short ", " added short ", " starter short ")
TRIM_HINTS = (" trim ", " trimmed ", " trimming ", " scaled ", " taking some off ", " reduced ")
FULL_EXIT_HINTS = (" sold ", " exit ", " exited ", " out of ", " cover ", " covered ", " closed ", " flat ")
EXIT_HINTS = TRIM_HINTS + FULL_EXIT_HINTS
STREAM_RULE_TAG = "velox_copy_trader_v2"

# Weights calibrated from 1-year backtest (data/backtest_results/copy_trader_backtest.json)
# InvestorsLive: 80% WR on 3d holds, +43.9% total, PF 2.17 — proven edge
# TraderStewie: Negative all hold periods, shorts especially bad (-164% on 5d)
# Others: Not yet backtested — keep at 1.0 baseline until data available
TRACKED_TRADERS = [
    {"handle": "TraderStewie", "name": "Gil Morales", "tier": "tier_1", "style": "momentum_swing", "weight": 0.5, "filter_shorts": True},
    {"handle": "InvestorsLive", "name": "Nathan Michaud", "tier": "tier_1", "style": "momentum", "weight": 2.0},
    {"handle": "markminervini", "name": "Mark Minervini", "tier": "tier_1", "style": "swing_growth", "weight": 1.0},
    {"handle": "PeterLBrandt", "name": "Peter Brandt", "tier": "tier_1", "style": "swing_classical", "weight": 1.0},
    {"handle": "alphatrends", "name": "Brian Shannon", "tier": "tier_1", "style": "momentum_swing", "weight": 1.0},
    {"handle": "ripster47", "name": "Ripster", "tier": "tier_1", "style": "swing_ema", "weight": 1.0},
]


class CopyTraderMonitor:
    """Monitor high-signal traders through X filtered stream with polling fallback."""

    def __init__(self):
        self._bearer = getattr(settings, "X_BEARER_TOKEN", "")
        self._mode = str(getattr(settings, "COPY_TRADER_MODE", "auto") or "auto").lower()
        if self._mode not in ("auto", "poll", "stream"):
            self._mode = "auto"
        self._cache: List[Dict] = []
        self._exit_cache: List[Dict] = []
        self._cache_ts = 0.0
        self._cache_ttl = 120
        self._signal_window_seconds = max(
            self._cache_ttl,
            int(getattr(settings, "COPY_TRADER_SIGNAL_WINDOW_SECONDS", 900) or 900),
        )
        self._stream_stale_seconds = max(
            30,
            int(getattr(settings, "COPY_TRADER_STREAM_STALE_SECONDS", 180) or 180),
        )
        self._stream_rule_refresh_seconds = max(
            300,
            int(getattr(settings, "COPY_TRADER_STREAM_RULE_REFRESH_SECONDS", 3600) or 3600),
        )
        self._seen_tweet_ids = set()
        self._signal_buffer: List[Dict] = []
        self._exit_buffer: List[Dict] = []
        self._traders = {row["handle"].lower(): dict(row) for row in TRACKED_TRADERS}
        self._performance_file = Path("data/copy_trader_performance.json")
        self._lock = threading.Lock()
        self._stream_stop = threading.Event()
        self._stream_thread: Optional[threading.Thread] = None
        self._stream_connected = False
        self._stream_last_event_ts = 0.0
        self._stream_last_error = ""
        self._stream_rules_checked_ts = 0.0
        self._session = requests.Session()
        self._session.headers.update(self._auth_headers())
        self._load_performance()
        if self._bearer:
            logger.info(f"Copy trader monitor initialized ({self._mode} mode)")
        else:
            logger.info("Copy trader monitor disabled (X_BEARER_TOKEN missing)")

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer}",
            "User-Agent": "Velox/1.0",
        }

    def is_configured(self) -> bool:
        return bool(self._bearer)

    def _stream_allowed(self) -> bool:
        return self.is_configured() and self._mode in ("auto", "stream")

    def _poll_allowed(self) -> bool:
        return self.is_configured() and self._mode in ("auto", "poll")

    def start_stream(self) -> bool:
        if not self._stream_allowed():
            return False
        if self._stream_thread and self._stream_thread.is_alive():
            return True
        self._stream_stop.clear()
        self._stream_thread = threading.Thread(
            target=self._run_stream_loop,
            name="copy-trader-stream",
            daemon=True,
        )
        self._stream_thread.start()
        return True

    def stop_stream(self):
        self._stream_stop.set()
        thread = self._stream_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._stream_connected = False

    def _ensure_streaming(self):
        if self._stream_allowed():
            self.start_stream()

    def _stream_is_fresh(self, now: Optional[float] = None) -> bool:
        now = float(now or time.time())
        if not self._stream_allowed():
            return False
        if not self._stream_connected:
            return False
        return (now - float(self._stream_last_event_ts or 0.0)) <= self._stream_stale_seconds

    def get_candidate_signals(self) -> List[Dict]:
        now = time.time()
        self._ensure_streaming()
        if self._stream_is_fresh(now):
            with self._lock:
                self._rebuild_aggregates_locked(now)
                return list(self._cache)
        if self._poll_allowed() and (now - self._cache_ts) >= self._cache_ttl:
            self._refresh_signal_caches(now)
        else:
            with self._lock:
                self._rebuild_aggregates_locked(now)
        return list(self._cache)

    def get_exit_signals(self) -> List[Dict]:
        now = time.time()
        self._ensure_streaming()
        if self._stream_is_fresh(now):
            with self._lock:
                self._rebuild_aggregates_locked(now)
                return list(self._exit_cache)
        if self._poll_allowed() and (now - self._cache_ts) >= self._cache_ttl:
            self._refresh_signal_caches(now)
        else:
            with self._lock:
                self._rebuild_aggregates_locked(now)
        return list(self._exit_cache)

    def _refresh_signal_caches(self, now: float):
        tweets = self._fetch_recent_tweets()
        ingested_signals = 0
        ingested_exits = 0
        for tweet in tweets:
            added_signals, added_exits = self._ingest_tweet(tweet, now=now)
            ingested_signals += added_signals
            ingested_exits += added_exits

        with self._lock:
            self._rebuild_aggregates_locked(now)
        self._save_performance()
        if ingested_signals:
            logger.info(f"📣 Copy trader signals: {len(self._cache)} candidates from Tier-1 handles")
        if ingested_exits:
            logger.info(f"📣 Copy trader exits: {len(self._exit_cache)} active symbol(s)")

    def _ingest_stream_payload(self, payload: Dict, now: Optional[float] = None) -> Tuple[int, int]:
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        includes = payload.get("includes", {}) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            return 0, 0

        author_id = str(data.get("author_id", "") or "")
        users = (includes.get("users", []) or []) if isinstance(includes, dict) else []
        user_map = {str(user.get("id", "")): str(user.get("username", "")).lower() for user in users}
        handle = user_map.get(author_id, "")
        if not handle:
            return 0, 0
        tweet = {
            "tweet_id": str(data.get("id", "") or ""),
            "text": str(data.get("text", "") or ""),
            "handle": handle,
            "created_at": str(data.get("created_at", "") or ""),
        }
        return self._ingest_tweet(tweet, now=now or time.time())

    def _ingest_tweet(self, tweet: Dict, now: float) -> Tuple[int, int]:
        tweet_id = str(tweet.get("tweet_id", "") or "")
        with self._lock:
            if tweet_id and tweet_id in self._seen_tweet_ids:
                return 0, 0
            if tweet_id:
                self._seen_tweet_ids.add(tweet_id)

        parsed_signals = self._parse_tweet(tweet)
        parsed_exits = self._parse_exit_tweet(tweet)

        with self._lock:
            for signal in parsed_signals:
                row = dict(signal)
                row["ingested_at"] = now
                self._signal_buffer.append(row)
            for signal in parsed_exits:
                row = dict(signal)
                row["ingested_at"] = now
                self._exit_buffer.append(row)
            self._rebuild_aggregates_locked(now)

        if parsed_signals:
            handle = str(tweet.get("handle", "")).lower()
            trader = self._traders.get(handle)
            if trader:
                trader["signals_emitted"] = int(trader.get("signals_emitted", 0) or 0) + len(parsed_signals)
                trader["last_signal_ts"] = now
        return len(parsed_signals), len(parsed_exits)

    def _prune_buffers_locked(self, now: float):
        cutoff = now - self._signal_window_seconds
        self._signal_buffer = [
            row for row in self._signal_buffer
            if float(row.get("ingested_at", 0) or 0) >= cutoff
        ]
        self._exit_buffer = [
            row for row in self._exit_buffer
            if float(row.get("ingested_at", 0) or 0) >= cutoff
        ]
        if len(self._seen_tweet_ids) > 20000:
            self._seen_tweet_ids = set(list(self._seen_tweet_ids)[-10000:])

    def _rebuild_aggregates_locked(self, now: float):
        self._prune_buffers_locked(now)

        grouped: Dict[Tuple[str, str], Dict] = {}
        for signal in self._signal_buffer:
            key = (signal["ticker"], signal["side"])
            bucket = grouped.setdefault(
                key,
                {
                    "symbol": signal["ticker"],
                    "price": 0,
                    "change_pct": 0,
                    "volume": 0,
                    "side": signal["side"],
                    "source": "copy_trader",
                    "copy_trader_signal_count": 0,
                    "copy_trader_handles": [],
                    "copy_trader_context": "",
                    "copy_trader_convergence": 0,
                    "copy_trader_weight": 1.0,
                    "copy_trader_size_multiplier": 1.0,
                    "copy_trader_score_adjustment": 0.0,
                    "priority": 1,
                    "copy_trader_stream_fresh": self._stream_is_fresh(now),
                },
            )
            bucket["copy_trader_signal_count"] += 1
            if signal["handle"] not in bucket["copy_trader_handles"]:
                bucket["copy_trader_handles"].append(signal["handle"])

        results = []
        for bucket in grouped.values():
            count = int(bucket["copy_trader_signal_count"] or 0)
            handles = bucket["copy_trader_handles"]
            avg_weight = self._average_weight(handles)
            base_adjustment = 0.12 + 0.04 * min(count, 3) + (max(0.0, avg_weight - 1.0) * 0.05)
            if count >= 3:
                base_adjustment += 0.08
            bucket["copy_trader_convergence"] = count
            bucket["copy_trader_weight"] = round(avg_weight, 3)
            bucket["copy_trader_size_multiplier"] = self._size_multiplier(count, avg_weight)
            bucket["copy_trader_score_adjustment"] = round(min(0.28, base_adjustment), 3)
            bucket["copy_trader_context"] = (
                f"{count} Tier-1 trader signal(s), avg weight {avg_weight:.2f}: "
                f"{', '.join(handles[:4])}"
            )
            results.append(bucket)

        grouped_exits: Dict[str, Dict] = {}
        for signal in self._exit_buffer:
            symbol = signal["ticker"]
            bucket = grouped_exits.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "copy_trader_exit_count": 0,
                    "copy_trader_exit_handles": [],
                    "copy_trader_exit_action": "trim",
                    "copy_trader_exit_context": "",
                    "copy_trader_exit_tweet_ids": [],
                    "copy_trader_exit_full_count": 0,
                    "copy_trader_exit_trim_count": 0,
                },
            )
            bucket["copy_trader_exit_count"] += 1
            if signal["handle"] not in bucket["copy_trader_exit_handles"]:
                bucket["copy_trader_exit_handles"].append(signal["handle"])
            if signal["tweet_id"] and signal["tweet_id"] not in bucket["copy_trader_exit_tweet_ids"]:
                bucket["copy_trader_exit_tweet_ids"].append(signal["tweet_id"])
            if signal["action"] == "exit":
                bucket["copy_trader_exit_full_count"] += 1
            else:
                bucket["copy_trader_exit_trim_count"] += 1

        exit_results = []
        for bucket in grouped_exits.values():
            count = int(bucket["copy_trader_exit_count"] or 0)
            full_count = int(bucket["copy_trader_exit_full_count"] or 0)
            trim_count = int(bucket["copy_trader_exit_trim_count"] or 0)
            if full_count and trim_count:
                action = "mixed"
            elif full_count:
                action = "exit"
            else:
                action = "trim"
            bucket["copy_trader_exit_action"] = action
            bucket["copy_trader_exit_context"] = (
                f"{count} Tier-1 {action} signal(s): {', '.join(bucket['copy_trader_exit_handles'][:4])}"
            )
            exit_results.append(bucket)

        results.sort(
            key=lambda row: (row.get("copy_trader_signal_count", 0), row.get("copy_trader_score_adjustment", 0.0)),
            reverse=True,
        )
        exit_results.sort(
            key=lambda row: (
                row.get("copy_trader_exit_full_count", 0),
                row.get("copy_trader_exit_count", 0),
            ),
            reverse=True,
        )
        self._cache = results
        self._exit_cache = exit_results
        self._cache_ts = now

    def _average_weight(self, handles: List[str]) -> float:
        weights = []
        for handle in handles:
            trader = self._traders.get(str(handle).lower())
            if trader:
                weights.append(float(trader.get("weight", 1.0) or 1.0))
        return sum(weights) / len(weights) if weights else 1.0

    @staticmethod
    def _size_multiplier(signal_count: int, avg_weight: float) -> float:
        multiplier = 1.0 + (0.04 * min(int(signal_count or 0), 3))
        multiplier += (float(avg_weight or 1.0) - 1.0) * 0.25
        if signal_count >= 3:
            multiplier += 0.03
        return round(max(0.75, min(1.25, multiplier)), 3)

    def _load_performance(self):
        try:
            if not self._performance_file.exists():
                return
            raw = json.loads(self._performance_file.read_text())
        except Exception:
            return

        traders = raw.get("traders", {}) if isinstance(raw, dict) else {}
        for handle, stats in traders.items():
            trader = self._traders.get(str(handle).lower())
            if not trader or not isinstance(stats, dict):
                continue
            trader["signals_emitted"] = int(stats.get("signals_emitted", trader.get("signals_emitted", 0)) or 0)
            trader["signals_correct"] = int(stats.get("signals_correct", trader.get("signals_correct", 0)) or 0)
            trader["signals_wrong"] = int(stats.get("signals_wrong", trader.get("signals_wrong", 0)) or 0)
            trader["current_streak"] = int(stats.get("current_streak", trader.get("current_streak", 0)) or 0)
            trader["weight"] = float(stats.get("weight", trader.get("weight", 1.0)) or 1.0)

    def _save_performance(self):
        try:
            self._performance_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "traders": {
                    handle: {
                        "signals_emitted": int(trader.get("signals_emitted", 0) or 0),
                        "signals_correct": int(trader.get("signals_correct", 0) or 0),
                        "signals_wrong": int(trader.get("signals_wrong", 0) or 0),
                        "current_streak": int(trader.get("current_streak", 0) or 0),
                        "weight": float(trader.get("weight", 1.0) or 1.0),
                    }
                    for handle, trader in self._traders.items()
                }
            }
            self._performance_file.write_text(json.dumps(payload, indent=2, sort_keys=True))
        except Exception as e:
            logger.debug(f"Copy trader performance save failed: {e}")

    def get_trader_stats(self) -> List[Dict]:
        rows = []
        for handle, trader in self._traders.items():
            correct = int(trader.get("signals_correct", 0) or 0)
            wrong = int(trader.get("signals_wrong", 0) or 0)
            resolved = correct + wrong
            realized_wr = (correct / resolved) if resolved else 0.5
            rows.append(
                {
                    "handle": handle,
                    "name": trader.get("name", handle),
                    "tier": trader.get("tier", "tier_1"),
                    "style": trader.get("style", ""),
                    "weight": round(float(trader.get("weight", 1.0) or 1.0), 3),
                    "signals_emitted": int(trader.get("signals_emitted", 0) or 0),
                    "signals_correct": correct,
                    "signals_wrong": wrong,
                    "resolved_trades": resolved,
                    "realized_win_rate": round(realized_wr, 3),
                    "current_streak": int(trader.get("current_streak", 0) or 0),
                }
            )
        rows.sort(key=lambda row: (row["weight"], row["signals_emitted"]), reverse=True)
        return rows

    def get_dashboard_data(self) -> Dict:
        now = time.time()
        return {
            "mode": self._mode,
            "stream_connected": self._stream_connected,
            "stream_fresh": self._stream_is_fresh(now),
            "last_stream_event_ts": self._stream_last_event_ts,
            "last_stream_error": self._stream_last_error,
            "signals": self.get_candidate_signals()[:5],
            "exits": self.get_exit_signals()[:5],
            "traders": self.get_trader_stats(),
        }

    def record_trade_result(self, trade_record: Dict):
        handles = trade_record.get("copy_trader_handles") or trade_record.get("copy_trader_handle") or []
        if isinstance(handles, str):
            handles = [h.strip().lower() for h in handles.split(",") if h.strip()]
        if not isinstance(handles, list):
            return

        try:
            pnl = float(trade_record.get("pnl", 0) or 0)
        except Exception:
            pnl = 0.0
        if pnl == 0:
            return

        changed = False
        for handle in handles:
            trader = self._traders.get(str(handle).lower())
            if not trader:
                continue
            if pnl > 0:
                trader["signals_correct"] = int(trader.get("signals_correct", 0) or 0) + 1
                streak = int(trader.get("current_streak", 0) or 0)
                trader["current_streak"] = streak + 1 if streak >= 0 else 1
            else:
                trader["signals_wrong"] = int(trader.get("signals_wrong", 0) or 0) + 1
                streak = int(trader.get("current_streak", 0) or 0)
                trader["current_streak"] = streak - 1 if streak <= 0 else -1

            resolved = int(trader.get("signals_correct", 0) or 0) + int(trader.get("signals_wrong", 0) or 0)
            if resolved >= 5:
                realized_wr = float(trader.get("signals_correct", 0) or 0) / max(1, resolved)
                weight = 1.0 + (realized_wr - 0.5)
                if int(trader.get("current_streak", 0) or 0) <= -3:
                    weight *= 0.8
                trader["weight"] = round(max(0.5, min(1.5, weight)), 3)
            changed = True

        if changed:
            self._save_performance()

    def _fetch_recent_tweets(self) -> List[Dict]:
        handles = list(self._traders.keys())
        query = "(" + " OR ".join(f"from:{handle}" for handle in handles) + ") lang:en -is:retweet"
        try:
            response = self._session.get(
                f"{BASE_URL}/tweets/search/recent",
                params={
                    "query": query,
                    "max_results": 100,
                    "tweet.fields": "created_at,text,author_id",
                    "expansions": "author_id",
                    "user.fields": "username,name",
                },
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            logger.debug(f"Copy trader tweet fetch failed: {e}")
            return []

        users = {
            str(user.get("id", "")): str(user.get("username", "")).lower()
            for user in ((payload.get("includes", {}) or {}).get("users", []) or [])
        }
        tweets = []
        for row in payload.get("data", []) or []:
            handle = users.get(str(row.get("author_id", "")), "")
            if not handle:
                continue
            tweets.append(
                {
                    "tweet_id": str(row.get("id", "")),
                    "text": str(row.get("text", "") or ""),
                    "handle": handle,
                    "created_at": str(row.get("created_at", "") or ""),
                }
            )
        return tweets

    def _build_stream_rule_value(self) -> str:
        handles = list(self._traders.keys())
        return "(" + " OR ".join(f"from:{handle}" for handle in handles) + ") lang:en -is:retweet"

    def _ensure_stream_rules(self, session: requests.Session) -> bool:
        now = time.time()
        if now - self._stream_rules_checked_ts < self._stream_rule_refresh_seconds:
            return True

        rule_url = f"{BASE_URL}/tweets/search/stream/rules"
        target_value = self._build_stream_rule_value()
        try:
            response = session.get(rule_url, timeout=15)
            response.raise_for_status()
            payload = response.json()
            existing_rules = payload.get("data", []) or []
            same_tag = [rule for rule in existing_rules if str(rule.get("tag", "")) == STREAM_RULE_TAG]
            if any(str(rule.get("value", "")) == target_value for rule in same_tag):
                self._stream_rules_checked_ts = now
                return True

            delete_ids = [str(rule.get("id", "")) for rule in same_tag if rule.get("id")]
            if delete_ids:
                delete_resp = session.post(rule_url, json={"delete": {"ids": delete_ids}}, timeout=15)
                delete_resp.raise_for_status()

            add_resp = session.post(
                rule_url,
                json={"add": [{"value": target_value, "tag": STREAM_RULE_TAG}]},
                timeout=15,
            )
            add_resp.raise_for_status()
            self._stream_rules_checked_ts = now
            return True
        except Exception as e:
            self._stream_last_error = str(e)
            logger.debug(f"Copy trader stream rules failed: {e}")
            return False

    def _run_stream_loop(self):
        backoff_seconds = 5.0
        while not self._stream_stop.is_set():
            session = requests.Session()
            session.headers.update(self._auth_headers())
            try:
                self._stream_connected = False
                if not self._ensure_stream_rules(session):
                    raise RuntimeError(self._stream_last_error or "stream rules unavailable")

                with session.get(
                    f"{BASE_URL}/tweets/search/stream",
                    params={
                        "tweet.fields": "created_at,text,author_id",
                        "expansions": "author_id",
                        "user.fields": "username,name",
                    },
                    stream=True,
                    timeout=(10, 90),
                ) as response:
                    response.raise_for_status()
                    self._stream_connected = True
                    self._stream_last_error = ""
                    self._stream_last_event_ts = time.time()
                    for line in response.iter_lines(decode_unicode=True):
                        if self._stream_stop.is_set():
                            break
                        self._stream_last_event_ts = time.time()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except Exception:
                            continue
                        self._ingest_stream_payload(payload, now=self._stream_last_event_ts)
                    self._stream_connected = False
                backoff_seconds = 5.0
            except Exception as e:
                self._stream_connected = False
                self._stream_last_error = str(e)
                logger.debug(f"Copy trader stream disconnected: {e}")
                if self._stream_stop.wait(backoff_seconds):
                    break
                backoff_seconds = min(60.0, backoff_seconds * 2.0)
            finally:
                try:
                    session.close()
                except Exception:
                    pass
        self._stream_connected = False

    def _parse_tweet(self, tweet: Dict) -> List[Dict]:
        text = f" {str(tweet.get('text', '') or '').lower()} "
        if any(exit_hint in text for exit_hint in EXIT_HINTS):
            return []

        side = ""
        if any(hint in text for hint in SHORT_HINTS):
            side = "short"
        elif any(hint in text for hint in BUY_HINTS):
            side = "long"
        if not side:
            return []

        tickers = []
        for ticker in TICKER_RE.findall(str(tweet.get("text", "") or "")):
            ticker = ticker.upper().strip()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
        if not tickers:
            return []

        handle = str(tweet.get("handle", "")).lower()
        trader = self._traders.get(handle, {})

        # Filter shorts for traders whose backtest shows negative short performance
        if side == "short" and trader.get("filter_shorts", False):
            logger.debug(f"Copy trader: filtering short from @{handle} (backtest-driven)")
            return []

        confidence = 0.62
        if any(char.isdigit() for char in text):
            confidence += 0.08
        if len(tickers) == 1:
            confidence += 0.05
        confidence *= float(trader.get("weight", 1.0) or 1.0)
        confidence = max(0.55, min(0.92, confidence))

        parsed = []
        for ticker in tickers[:3]:
            parsed.append(
                {
                    "ticker": ticker,
                    "side": side,
                    "handle": handle,
                    "tweet_id": tweet.get("tweet_id", ""),
                    "tweet_text": tweet.get("text", ""),
                    "confidence": round(confidence, 3),
                }
            )
        return parsed

    def _parse_exit_tweet(self, tweet: Dict) -> List[Dict]:
        text = f" {str(tweet.get('text', '') or '').lower()} "
        action = ""
        if any(hint in text for hint in FULL_EXIT_HINTS):
            action = "exit"
        elif any(hint in text for hint in TRIM_HINTS):
            action = "trim"
        if not action:
            return []

        tickers = []
        for ticker in TICKER_RE.findall(str(tweet.get("text", "") or "")):
            ticker = ticker.upper().strip()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
        if not tickers:
            return []

        handle = str(tweet.get("handle", "")).lower()
        return [
            {
                "ticker": ticker,
                "action": action,
                "handle": handle,
                "tweet_id": tweet.get("tweet_id", ""),
                "tweet_text": tweet.get("text", ""),
            }
            for ticker in tickers[:3]
        ]
