"""
Human intelligence store.

Persists operator-supplied context such as article links, chart notes,
Discord rumors, or other discretionary inputs that should influence scanning
and jury prompts for a ticker.
"""

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger


DATA_DIR = Path(__file__).parent.parent.parent / "data"
HUMAN_INTEL_FILE = DATA_DIR / "human_intel.json"
DEFAULT_TTL_HOURS = 96.0
MAX_ACTIVE_ENTRIES = 250
MARKET_TICKER = "MARKET"


class HumanIntelStore:
    """Persist and summarize discretionary operator context."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or HUMAN_INTEL_FILE)
        self._entries: List[Dict] = []
        self._load()
        self._prune_expired(save=False)
        logger.info(f"Human intel store initialized ({len(self._entries)} active entries)")

    def _load(self):
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text())
            entries = raw.get("entries", raw if isinstance(raw, list) else [])
            if not isinstance(entries, list):
                return
            self._entries = [self._normalize_entry(entry) for entry in entries if isinstance(entry, dict)]
        except Exception as e:
            logger.debug(f"Human intel load failed: {e}")
            self._entries = []

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "entries": self._entries,
                        "updated_at": datetime.utcnow().isoformat() + "Z",
                        "count": len(self._entries),
                    },
                    indent=2,
                )
            )
        except Exception as e:
            logger.debug(f"Human intel save failed: {e}")

    @staticmethod
    def _clamp_confidence(value) -> float:
        try:
            return max(0.05, min(1.0, float(value)))
        except Exception:
            return 0.5

    @staticmethod
    def _normalize_bias(value: str) -> str:
        raw = str(value or "").strip().lower()
        if raw in ("bull", "bullish", "long", "buy"):
            return "bullish"
        if raw in ("bear", "bearish", "short", "sell"):
            return "bearish"
        return "neutral"

    @staticmethod
    def _normalize_kind(value: str) -> str:
        raw = str(value or "").strip().lower()
        return raw or "note"

    def _normalize_entry(self, entry: Dict) -> Dict:
        now = time.time()
        created_at = float(entry.get("created_at", now) or now)
        ttl_hours = float(entry.get("ttl_hours", DEFAULT_TTL_HOURS) or DEFAULT_TTL_HOURS)
        expires_at = float(entry.get("expires_at", created_at + ttl_hours * 3600) or (created_at + ttl_hours * 3600))
        ticker = str(entry.get("ticker", MARKET_TICKER) or MARKET_TICKER).upper().strip()
        if not ticker:
            ticker = MARKET_TICKER
        return {
            "id": str(entry.get("id") or uuid.uuid4().hex),
            "ticker": ticker,
            "title": str(entry.get("title", "") or "").strip(),
            "notes": str(entry.get("notes", "") or "").strip(),
            "url": str(entry.get("url", "") or "").strip(),
            "source": str(entry.get("source", "") or "").strip(),
            "kind": self._normalize_kind(entry.get("kind", "note")),
            "bias": self._normalize_bias(entry.get("bias", "neutral")),
            "confidence": round(self._clamp_confidence(entry.get("confidence", 0.5)), 3),
            "created_at": created_at,
            "expires_at": expires_at,
            "ttl_hours": round(ttl_hours, 2),
        }

    def _prune_expired(self, save: bool = True):
        now = time.time()
        before = len(self._entries)
        self._entries = [entry for entry in self._entries if float(entry.get("expires_at", 0) or 0) > now]
        if save and len(self._entries) != before:
            self._save()

    def add_entry(
        self,
        ticker: str,
        title: str = "",
        notes: str = "",
        url: str = "",
        source: str = "",
        kind: str = "note",
        bias: str = "neutral",
        confidence: float = 0.5,
        ttl_hours: float = DEFAULT_TTL_HOURS,
    ) -> Dict:
        entry = self._normalize_entry(
            {
                "ticker": ticker,
                "title": title,
                "notes": notes,
                "url": url,
                "source": source,
                "kind": kind,
                "bias": bias,
                "confidence": confidence,
                "ttl_hours": ttl_hours,
            }
        )
        self._prune_expired(save=False)
        self._entries.insert(0, entry)
        self._entries = self._entries[:MAX_ACTIVE_ENTRIES]
        self._save()
        return entry

    def remove_entry(self, entry_id: str) -> bool:
        before = len(self._entries)
        self._entries = [entry for entry in self._entries if entry.get("id") != entry_id]
        removed = len(self._entries) != before
        if removed:
            self._save()
        return removed

    def list_entries(self, ticker: Optional[str] = None, limit: Optional[int] = None) -> List[Dict]:
        self._prune_expired(save=False)
        target = str(ticker or "").upper().strip()
        entries = [
            dict(entry)
            for entry in self._entries
            if not target or str(entry.get("ticker", "")).upper() == target
        ]
        entries.sort(key=lambda item: float(item.get("created_at", 0) or 0), reverse=True)
        if limit is not None:
            return entries[: max(0, int(limit))]
        return entries

    def summarize_for_symbol(self, symbol: str) -> Dict:
        symbol = str(symbol or "").upper().strip()
        entries = self.list_entries(symbol)
        if not entries:
            return {
                "ticker": symbol,
                "count": 0,
                "bias": "neutral",
                "avg_confidence": 0.0,
                "score_adjustment": 0.0,
                "summary": "",
                "entries": [],
            }

        bullish = sum(float(entry.get("confidence", 0) or 0) for entry in entries if entry.get("bias") == "bullish")
        bearish = sum(float(entry.get("confidence", 0) or 0) for entry in entries if entry.get("bias") == "bearish")
        avg_conf = sum(float(entry.get("confidence", 0) or 0) for entry in entries) / max(1, len(entries))
        if bullish > (bearish * 1.1):
            bias = "bullish"
        elif bearish > (bullish * 1.1):
            bias = "bearish"
        else:
            bias = "neutral"

        score_adjustment = 0.0
        if bias == "bullish":
            score_adjustment = min(0.18, 0.08 + avg_conf * 0.10)
        elif bias == "bearish":
            score_adjustment = -min(0.18, 0.08 + avg_conf * 0.10)

        top_lines = []
        for entry in entries[:3]:
            label = entry.get("title") or entry.get("notes") or entry.get("source") or entry.get("kind")
            label = str(label or "").strip()
            if len(label) > 80:
                label = f"{label[:77]}..."
            prefix = entry.get("kind", "note")
            if entry.get("source"):
                prefix = f"{prefix}/{entry['source']}"
            top_lines.append(f"{prefix}: {label}".strip(": "))

        summary = (
            f"{len(entries)} operator notes; bias={bias}; conf={avg_conf:.2f}; "
            + " | ".join(top_lines)
        )
        return {
            "ticker": symbol,
            "count": len(entries),
            "bias": bias,
            "avg_confidence": round(avg_conf, 3),
            "score_adjustment": round(score_adjustment, 3),
            "summary": summary[:280],
            "entries": entries[:10],
        }

    def get_watchlist_candidates(self, limit: int = 20) -> List[Dict]:
        self._prune_expired(save=False)
        seen = []
        tickers = []
        for entry in self._entries:
            ticker = str(entry.get("ticker", "")).upper()
            if not ticker or ticker == MARKET_TICKER or ticker in seen:
                continue
            seen.append(ticker)
            tickers.append(ticker)
        summaries = [self.summarize_for_symbol(ticker) for ticker in tickers]
        summaries = [summary for summary in summaries if summary.get("count", 0) > 0]
        summaries.sort(
            key=lambda row: (float(row.get("avg_confidence", 0) or 0), int(row.get("count", 0) or 0)),
            reverse=True,
        )
        return summaries[: max(0, int(limit))]
