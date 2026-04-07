"""
Session Context Stack — Bloomberg Morning Stack equivalent.

Produces a deterministic SessionContextSnapshot every refresh cycle that feeds
into mode classification, play resolution, entry sizing, and gating rules.

Integrates existing modules: OvernightContext, SectorRotationModel, FredClient,
FinnhubClient.  No new external data dependencies — everything is already wired.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger


REFRESH_TTL_SECONDS = 300  # 5 min default

VIX_REGIME_THRESHOLDS = {"low": 15, "normal": 20, "elevated": 30}
SKEW_REGIME_THRESHOLDS = {"normal": 130, "elevated": 145}


@dataclass
class SessionContextSnapshot:
    timestamp: float = 0.0
    session_label: str = "unknown"

    # Index Context (GMM equivalent)
    overnight_index_bias: str = "flat"
    spy_change_pct: float = 0.0
    qqq_change_pct: float = 0.0
    iwm_change_pct: float = 0.0
    dia_change_pct: float = 0.0
    spy_vs_200ma: str = "unknown"
    qqq_vs_200ma: str = "unknown"

    # Rates Context (BTMM equivalent)
    rates_regime: str = "neutral"
    fed_funds_rate: float = 0.0
    yield_curve_10y2y: float = 0.0
    yield_curve_inverted: bool = False
    cpi_yoy: float = 0.0

    # Volatility Context
    vix_level: float = 0.0
    vix_regime: str = "normal"
    vix_term_structure: str = "unknown"
    cboe_skew: float = 0.0
    skew_regime: str = "normal"

    # Sector Context
    sector_leaders: List[str] = field(default_factory=list)
    sector_laggards: List[str] = field(default_factory=list)
    sector_bias: str = "neutral"

    # Event Context
    macro_events_today: List[str] = field(default_factory=list)
    fed_speaking_today: bool = False

    # Composite
    broad_risk_tone: str = "neutral"
    market_regime: str = "mixed"
    regime_confidence: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "session_label": self.session_label,
            "overnight_index_bias": self.overnight_index_bias,
            "spy_change_pct": self.spy_change_pct,
            "qqq_change_pct": self.qqq_change_pct,
            "iwm_change_pct": self.iwm_change_pct,
            "dia_change_pct": self.dia_change_pct,
            "spy_vs_200ma": self.spy_vs_200ma,
            "qqq_vs_200ma": self.qqq_vs_200ma,
            "rates_regime": self.rates_regime,
            "fed_funds_rate": self.fed_funds_rate,
            "yield_curve_10y2y": self.yield_curve_10y2y,
            "yield_curve_inverted": self.yield_curve_inverted,
            "cpi_yoy": self.cpi_yoy,
            "vix_level": self.vix_level,
            "vix_regime": self.vix_regime,
            "vix_term_structure": self.vix_term_structure,
            "cboe_skew": self.cboe_skew,
            "skew_regime": self.skew_regime,
            "sector_leaders": list(self.sector_leaders),
            "sector_laggards": list(self.sector_laggards),
            "sector_bias": self.sector_bias,
            "macro_events_today": list(self.macro_events_today),
            "fed_speaking_today": self.fed_speaking_today,
            "broad_risk_tone": self.broad_risk_tone,
            "market_regime": self.market_regime,
            "regime_confidence": self.regime_confidence,
        }


def _classify_vix_regime(vix: float) -> str:
    if vix <= 0:
        return "unknown"
    if vix < VIX_REGIME_THRESHOLDS["low"]:
        return "low"
    if vix < VIX_REGIME_THRESHOLDS["normal"]:
        return "normal"
    if vix < VIX_REGIME_THRESHOLDS["elevated"]:
        return "elevated"
    return "extreme"


def _classify_skew_regime(skew: float) -> str:
    if skew <= 0:
        return "unknown"
    if skew < SKEW_REGIME_THRESHOLDS["normal"]:
        return "normal"
    if skew < SKEW_REGIME_THRESHOLDS["elevated"]:
        return "elevated"
    return "extreme"


def _classify_vix_term_structure(vix_spot: float, vix_3m: float) -> str:
    if vix_3m <= 0 or vix_spot <= 0:
        return "unknown"
    ratio = vix_spot / vix_3m
    if ratio > 1.05:
        return "backwardation"
    if ratio < 0.95:
        return "contango"
    return "flat"


def _classify_rates_regime(fed_funds: float, yield_curve: float) -> str:
    if fed_funds >= 4.5 and yield_curve < 0:
        return "tightening"
    if fed_funds <= 2.5 and yield_curve > 0.5:
        return "easing"
    return "neutral"


def _compute_risk_tone(
    overnight_bias: str,
    vix_regime: str,
    vix_term: str,
    yield_inverted: bool,
    rates_regime: str,
    skew_regime: str,
) -> tuple[str, float]:
    """Compute composite broad_risk_tone and regime_confidence."""
    risk_off_signals = 0
    risk_on_signals = 0
    total_signals = 0

    if overnight_bias == "bearish":
        risk_off_signals += 1
    elif overnight_bias == "bullish":
        risk_on_signals += 1
    total_signals += 1

    if vix_regime in ("elevated", "extreme"):
        risk_off_signals += 2 if vix_regime == "extreme" else 1
    elif vix_regime == "low":
        risk_on_signals += 1
    total_signals += 1

    if vix_term == "backwardation":
        risk_off_signals += 2
    elif vix_term == "contango":
        risk_on_signals += 1
    total_signals += 1

    if yield_inverted:
        risk_off_signals += 1
    total_signals += 1

    if rates_regime == "tightening":
        risk_off_signals += 1
    elif rates_regime == "easing":
        risk_on_signals += 1
    total_signals += 1

    if skew_regime == "extreme":
        risk_off_signals += 1
    total_signals += 1

    score = risk_on_signals - risk_off_signals
    max_possible = total_signals
    confidence = abs(score) / max(1, max_possible)

    if score <= -2:
        return "risk_off", round(min(1.0, confidence), 3)
    if score >= 2:
        return "risk_on", round(min(1.0, confidence), 3)
    return "neutral", round(min(1.0, confidence), 3)


class SessionContext:
    """
    Builds and caches a SessionContextSnapshot from existing data modules.

    Usage:
        ctx = SessionContext(fred_client, polygon_client, finnhub_client,
                             overnight_context, sector_model)
        await ctx.refresh()
        snapshot = ctx.snapshot
    """

    def __init__(
        self,
        fred_client=None,
        polygon_client=None,
        finnhub_client=None,
        overnight_context=None,
        sector_model=None,
        state_store=None,
    ):
        self._fred = fred_client
        self._polygon = polygon_client
        self._finnhub = finnhub_client
        self._overnight = overnight_context
        self._sector = sector_model
        self._state_store = state_store
        self._snapshot = SessionContextSnapshot()
        self._last_refresh = 0.0

    @property
    def snapshot(self) -> SessionContextSnapshot:
        return self._snapshot

    def is_stale(self, ttl: int = REFRESH_TTL_SECONDS) -> bool:
        return (time.time() - self._last_refresh) > ttl

    async def refresh(self) -> SessionContextSnapshot:
        """Rebuild the session context from all sources.
        Each data source is independently try/excepted so a single
        slow or broken provider never kills the whole refresh."""
        import asyncio
        snap = SessionContextSnapshot(timestamp=time.time())
        loop = asyncio.get_event_loop()
        for populate_fn in (
            self._populate_overnight,
            self._populate_fred,
            self._populate_sectors,
            self._populate_events,
            self._populate_vix,
        ):
            try:
                await loop.run_in_executor(None, populate_fn, snap)
            except Exception as e:
                logger.debug(f"SessionContext {populate_fn.__name__} failed: {e}")
        try:
            self._compute_composites(snap)
        except Exception as e:
            logger.warning(f"SessionContext composite computation failed: {e}")

        self._snapshot = snap
        self._last_refresh = time.time()
        logger.info(
            f"🌍 SessionContext refreshed: risk_tone={snap.broad_risk_tone} "
            f"vix={snap.vix_level:.1f}({snap.vix_regime}) "
            f"yield_curve={snap.yield_curve_10y2y:+.2f} "
            f"overnight={snap.overnight_index_bias} "
            f"conf={snap.regime_confidence:.2f}"
        )

        if self._state_store:
            try:
                self._state_store.save_session_snapshot(snap.to_dict())
            except Exception as e:
                logger.debug(f"SessionContext snapshot save to SQLite failed: {e}")

        return snap

    def _populate_overnight(self, snap: SessionContextSnapshot):
        if not self._overnight:
            return
        bias = self._overnight.get_bias(refresh=True)
        snap.overnight_index_bias = str(bias.get("direction", "flat"))
        snap.spy_change_pct = float(bias.get("spy_change_pct", 0.0) or 0.0)
        snap.qqq_change_pct = float(bias.get("qqq_change_pct", 0.0) or 0.0)
        snap.iwm_change_pct = float(bias.get("iwm_change_pct", 0.0) or 0.0)
        snap.dia_change_pct = float(bias.get("dia_change_pct", 0.0) or 0.0)
        snap.session_label = str(bias.get("session", "unknown"))

    def _populate_fred(self, snap: SessionContextSnapshot):
        if not self._fred or not self._fred.is_configured():
            return
        macro = self._fred.get_macro_snapshot()
        snap.fed_funds_rate = float(macro.get("fed_funds", 0.0) or 0.0)
        snap.yield_curve_10y2y = float(macro.get("yield_curve_10y2y", 0.0) or 0.0)
        snap.yield_curve_inverted = snap.yield_curve_10y2y < 0
        snap.cpi_yoy = float(macro.get("cpi_yoy", 0.0) or 0.0)
        snap.rates_regime = _classify_rates_regime(snap.fed_funds_rate, snap.yield_curve_10y2y)

        vix = self._get_vix_live()
        if vix is not None:
            snap.vix_level = vix
            snap.vix_regime = _classify_vix_regime(vix)

        skew = self._get_skew_from_fred()
        if skew is not None:
            snap.cboe_skew = skew
            snap.skew_regime = _classify_skew_regime(skew)

        vix_3m = self._get_vix_3m_from_fred()
        if vix_3m is not None and snap.vix_level > 0:
            snap.vix_term_structure = _classify_vix_term_structure(snap.vix_level, vix_3m)

    def _get_vix_live(self) -> Optional[float]:
        """Get live VIX. Polygon/Alpaca first (real-time), FRED fallback (delayed 1+ day)."""
        if self._polygon:
            for ticker in ("VIXY", "VIX", "UVXY"):
                try:
                    price = self._polygon.get_price(ticker)
                    if price and price > 0:
                        if ticker == "UVXY":
                            return float(price) * 0.5
                        if ticker == "VIXY":
                            return float(price) * 1.1
                        return float(price)
                except Exception:
                    continue
        if self._fred:
            try:
                obs = self._fred.get_series_observations("VIXCLS", limit=1)
                if obs:
                    val = self._fred._to_float(obs[0].get("value"))
                    if val is not None:
                        return val
            except Exception:
                pass
        return None

    def _get_vix_3m_from_fred(self) -> Optional[float]:
        if not self._fred:
            return None
        try:
            obs = self._fred.get_series_observations("VXVCLS", limit=1)
            if obs:
                return self._fred._to_float(obs[0].get("value"))
        except Exception:
            pass
        return None

    def _get_skew_from_fred(self) -> Optional[float]:
        if not self._fred:
            return None
        try:
            obs = self._fred.get_series_observations("SKEW", limit=1)
            if obs:
                return self._fred._to_float(obs[0].get("value"))
        except Exception:
            pass
        return None

    def _populate_sectors(self, snap: SessionContextSnapshot):
        if not self._sector:
            return
        try:
            hot = self._sector.get_hot_sectors()
            cold = self._sector.get_cold_sectors()
            snap.sector_leaders = [s.get("sector", s.get("etf", "")) for s in (hot or [])[:3]]
            snap.sector_laggards = [s.get("sector", s.get("etf", "")) for s in (cold or [])[:3]]
            snap.sector_bias = str(self._sector.get_sector_bias() or "neutral")
        except Exception as e:
            logger.debug(f"SessionContext sector population failed: {e}")

    def _populate_events(self, snap: SessionContextSnapshot):
        if not self._finnhub or not self._finnhub.is_configured():
            return
        try:
            calendar = self._finnhub.get_economic_calendar()
            events = calendar.get("economicCalendar", {}).get("result", [])
            if isinstance(events, list):
                snap.macro_events_today = [
                    f"{e.get('event', 'Unknown')} ({e.get('impact', 'low')})"
                    for e in events[:10]
                ]
                fed_keywords = {"fomc", "federal reserve", "fed chair", "powell", "rate decision"}
                snap.fed_speaking_today = any(
                    any(kw in str(e.get("event", "")).lower() for kw in fed_keywords)
                    for e in events
                )
        except Exception as e:
            logger.debug(f"SessionContext events population failed: {e}")

    def _populate_vix(self, snap: SessionContextSnapshot):
        """Fallback VIX from Polygon if FRED didn't provide it."""
        if snap.vix_level > 0:
            return
        if not self._polygon:
            return
        try:
            price = self._polygon.get_price("UVXY")
            if price and price > 0:
                snap.vix_level = float(price) * 0.5
                snap.vix_regime = _classify_vix_regime(snap.vix_level)
        except Exception:
            pass

    def _compute_composites(self, snap: SessionContextSnapshot):
        tone, confidence = _compute_risk_tone(
            snap.overnight_index_bias,
            snap.vix_regime,
            snap.vix_term_structure,
            snap.yield_curve_inverted,
            snap.rates_regime,
            snap.skew_regime,
        )
        snap.broad_risk_tone = tone
        snap.regime_confidence = confidence

        if tone == "risk_off":
            snap.market_regime = "risk_off"
        elif tone == "risk_on":
            snap.market_regime = "risk_on"
        elif snap.overnight_index_bias == "flat" and snap.vix_regime in ("normal", "low"):
            snap.market_regime = "choppy"
        else:
            snap.market_regime = "mixed"

    def get_sizing_modifier(self) -> float:
        """Return a position size multiplier based on current context."""
        snap = self._snapshot
        modifier = 1.0
        if snap.broad_risk_tone == "risk_off":
            modifier *= 0.50
        elif snap.broad_risk_tone == "neutral":
            modifier *= 0.85
        if snap.vix_regime == "extreme":
            modifier *= 0.50
        elif snap.vix_regime == "elevated":
            modifier *= 0.75
        if snap.skew_regime == "extreme":
            modifier *= 0.75
        if snap.fed_speaking_today:
            modifier *= 0.85
        return round(max(0.1, modifier), 3)

    def should_block_longs(self) -> tuple[bool, str]:
        """Check if long entries should be blocked."""
        snap = self._snapshot
        if snap.vix_regime == "extreme":
            return True, "vix_extreme"
        if snap.spy_vs_200ma == "below" and snap.broad_risk_tone == "risk_off":
            return True, "spy_below_200ma_risk_off"
        return False, ""

    def get_confidence_adjustment(self, mode: str) -> float:
        """Return a confidence adjustment for the mode classifier based on context."""
        snap = self._snapshot
        adj = 0.0
        is_long = "long" in mode
        is_short = "short" in mode

        if snap.broad_risk_tone == "risk_off":
            if is_long:
                adj -= 0.15
            if mode == "exhaustion_fade_short":
                adj += 0.10
        elif snap.broad_risk_tone == "risk_on":
            if is_long:
                adj += 0.05

        if snap.vix_regime == "extreme" and is_long:
            adj -= 0.10

        if snap.spy_vs_200ma == "below" and is_long:
            adj -= 0.20

        if snap.vix_term_structure == "backwardation":
            if is_long:
                adj -= 0.10
            if is_short:
                adj += 0.05

        return round(adj, 3)
