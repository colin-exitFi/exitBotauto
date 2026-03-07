"""
Persistent strategy disable/enable control plane.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set


DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CONTROLS_FILE = DATA_DIR / "strategy_controls.json"

_DEFAULT_CONTROLS = {
    "hard_disabled": {},
    "manual_enabled": {},
    "manual_disabled": {},
    "soft_disabled": {},
    "size_reductions": {},
    "probation": {},
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_controls(raw: Dict = None) -> Dict:
    controls = deepcopy(_DEFAULT_CONTROLS)
    if isinstance(raw, dict):
        for key in controls.keys():
            value = raw.get(key, {})
            controls[key] = value if isinstance(value, dict) else {}
    return controls


def load_controls() -> Dict:
    """Read data/strategy_controls.json (or return defaults if absent)."""
    DATA_DIR.mkdir(exist_ok=True)
    if not CONTROLS_FILE.exists():
        return _normalize_controls()
    try:
        raw = json.loads(CONTROLS_FILE.read_text())
    except Exception:
        return _normalize_controls()
    return _normalize_controls(raw)


def save_controls(controls: Dict) -> Dict:
    """Write data/strategy_controls.json."""
    normalized = _normalize_controls(controls)
    DATA_DIR.mkdir(exist_ok=True)
    CONTROLS_FILE.write_text(json.dumps(normalized, indent=2, sort_keys=True))
    return normalized


def apply_auto_disables(recommendations: List, controls: Dict) -> Dict:
    """
    Merge game-film disable recommendations into hard_disabled.
    Manual enables remain sticky until explicitly revoked.
    """
    merged = _normalize_controls(controls)
    hard_disabled = merged["hard_disabled"]
    manual_enabled = merged["manual_enabled"]

    for rec in recommendations or []:
        if isinstance(rec, str):
            tag = rec.strip()
            reason = ""
            trades = 0
            win_rate = 0.0
            pnl = 0.0
        elif isinstance(rec, dict):
            tag = str(rec.get("strategy_tag") or rec.get("tag") or "").strip()
            reason = str(rec.get("reason") or "").strip()
            trades = int(rec.get("trades", 0) or 0)
            win_rate = float(rec.get("win_rate_pct", rec.get("win_rate", 0)) or 0)
            pnl = float(rec.get("pnl", 0) or 0)
        else:
            continue

        if not tag:
            continue
        if tag in manual_enabled:
            # Human override stays in force until explicitly removed.
            continue

        if not reason:
            reason = f"win_rate={win_rate:.1f}%, pnl=${pnl:.2f}, trades={trades}"

        hard_disabled[tag] = {
            "reason": reason,
            "disabled_at": _utc_now_iso(),
            "disabled_by": "game_film",
            "trades": trades,
            "win_rate_pct": round(win_rate, 2),
            "pnl": round(pnl, 2),
        }

    return merged


def apply_recommendations(recommendations: Dict, controls: Dict) -> Dict:
    merged = _normalize_controls(controls)
    if not isinstance(recommendations, dict):
        return merged

    merged = apply_auto_disables(recommendations.get("disable_strategies", []), merged)
    manual_enabled = merged["manual_enabled"]

    for rec in recommendations.get("soft_disable_strategies", []) or []:
        tag = str(rec.get("strategy_tag") or "").strip()
        if not tag or tag in manual_enabled:
            continue
        merged["soft_disabled"][tag] = {
            "reason": str(rec.get("reason") or "").strip() or "Soft disabled by game film",
            "disabled_at": _utc_now_iso(),
            "disabled_by": "game_film",
            "trades": int(rec.get("trades", 0) or 0),
            "win_rate_pct": float(rec.get("win_rate_pct", 0) or 0),
            "pnl": float(rec.get("pnl", 0) or 0),
        }

    for rec in recommendations.get("size_reductions", []) or []:
        tag = str(rec.get("strategy_tag") or "").strip()
        if not tag:
            continue
        merged["size_reductions"][tag] = {
            "multiplier": max(0.1, min(1.0, float(rec.get("size_multiplier", 1.0) or 1.0))),
            "reason": str(rec.get("reason") or "").strip(),
            "updated_at": _utc_now_iso(),
        }

    for rec in recommendations.get("probation_candidates", []) or []:
        tag = str(rec.get("strategy_tag") or "").strip()
        if not tag:
            continue
        merged["probation"][tag] = {
            "started_at": _utc_now_iso(),
            "size_mult": max(0.1, min(1.0, float(rec.get("probation_size_mult", 0.25) or 0.25))),
            "reason": str(rec.get("reason") or "").strip(),
            "status": "active",
        }
        merged["hard_disabled"].pop(tag, None)
        merged["soft_disabled"].pop(tag, None)

    for rec in recommendations.get("probation_passed", []) or []:
        tag = str(rec.get("strategy_tag") or "").strip()
        if not tag:
            continue
        merged["probation"].pop(tag, None)
        merged["hard_disabled"].pop(tag, None)
        merged["soft_disabled"].pop(tag, None)
        merged["size_reductions"].pop(tag, None)

    for rec in recommendations.get("probation_failed", []) or []:
        tag = str(rec.get("strategy_tag") or "").strip()
        if not tag:
            continue
        merged["probation"].pop(tag, None)
        merged["hard_disabled"][tag] = {
            "reason": str(rec.get("reason") or "").strip() or "Probation failed",
            "disabled_at": _utc_now_iso(),
            "disabled_by": "probation_failure",
            "trades": int(rec.get("trades", 0) or 0),
            "win_rate_pct": float(rec.get("win_rate_pct", 0) or 0),
            "pnl": float(rec.get("pnl", 0) or 0),
        }

    return merged


def get_effective_disabled(controls: Dict) -> Set[str]:
    """Effective disabled set with manual overrides and probation exceptions."""
    normalized = _normalize_controls(controls)
    hard_disabled = set(normalized["hard_disabled"].keys())
    soft_disabled = set(normalized["soft_disabled"].keys())
    manual_disabled = set(normalized["manual_disabled"].keys())
    manual_enabled = set(normalized["manual_enabled"].keys())
    probation_active = {
        tag
        for tag, entry in normalized["probation"].items()
        if isinstance(entry, dict) and str(entry.get("status", "active")) == "active"
    }
    return (hard_disabled | soft_disabled | manual_disabled) - manual_enabled - probation_active


def get_size_multiplier(tag: str, controls: Dict) -> float:
    normalized = _normalize_controls(controls)
    tag = str(tag or "").strip()
    if not tag:
        return 1.0

    multiplier = 1.0
    reduction = normalized["size_reductions"].get(tag)
    if isinstance(reduction, dict):
        try:
            multiplier *= float(reduction.get("multiplier", 1.0) or 1.0)
        except Exception:
            pass

    probation = normalized["probation"].get(tag)
    if isinstance(probation, dict) and str(probation.get("status", "active")) == "active":
        try:
            multiplier *= float(probation.get("size_mult", 0.25) or 0.25)
        except Exception:
            pass

    return max(0.1, min(1.0, float(multiplier)))


def manual_disable(tag: str, reason: str, controls: Dict) -> Dict:
    """Manual disable wins over any previous manual-enable for the same tag."""
    merged = _normalize_controls(controls)
    tag = str(tag or "").strip()
    if not tag:
        return merged
    merged["manual_enabled"].pop(tag, None)
    merged["manual_disabled"][tag] = {
        "reason": str(reason or "").strip() or "Manual disable",
        "disabled_at": _utc_now_iso(),
        "disabled_by": "dashboard",
    }
    return merged


def manual_enable(tag: str, reason: str, controls: Dict) -> Dict:
    """Manual enable clears any conflicting manual-disable for the same tag."""
    merged = _normalize_controls(controls)
    tag = str(tag or "").strip()
    if not tag:
        return merged
    merged["manual_disabled"].pop(tag, None)
    merged["manual_enabled"][tag] = {
        "reason": str(reason or "").strip() or "Manual enable",
        "enabled_at": _utc_now_iso(),
        "enabled_by": "dashboard",
    }
    return merged
