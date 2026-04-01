"""
Governance summary and weekly committee memo generation.

This module turns existing analytics, controls, reconciliation, and change
history into operator-facing governance outputs.
"""

import time
from typing import Dict, List

from src import persistence
from src.ai import trade_history
from src.data import strategy_controls


def _top_books(rows: List[Dict], *, positive: bool, limit: int = 3) -> List[Dict]:
    filtered = []
    for row in rows or []:
        pnl = float(row.get("pnl", 0.0) or 0.0)
        if positive and pnl > 0:
            filtered.append(row)
        if not positive and pnl < 0:
            filtered.append(row)
    filtered.sort(key=lambda row: float(row.get("pnl", 0.0) or 0.0), reverse=positive)
    return filtered[:limit]


def _reconciliation_summary(state: Dict) -> Dict:
    state = state if isinstance(state, dict) else {}
    recon = dict(state.get("reconciliation", {}) or {})
    trust = dict(state.get("trust", {}) or {})
    canaries = list(state.get("canaries", []) or [])
    critical = [row for row in canaries if str(row.get("severity", "")).lower() == "critical"]
    warnings = [row for row in canaries if str(row.get("severity", "")).lower() == "warning"]
    return {
        "status": str(recon.get("status", "unknown") or "unknown"),
        "severity": str(recon.get("severity", "unknown") or "unknown"),
        "reasons": list(recon.get("reasons", []) or []),
        "critical_canaries": critical,
        "warning_canaries": warnings,
        "trust_flags": trust,
    }


def _controls_summary(controls: Dict) -> Dict:
    controls = controls if isinstance(controls, dict) else {}
    probation = {
        tag: entry
        for tag, entry in dict(controls.get("probation", {}) or {}).items()
        if isinstance(entry, dict) and str(entry.get("status", "active") or "active") == "active"
    }
    return {
        "probation": probation,
        "hard_disabled": dict(controls.get("hard_disabled", {}) or {}),
        "soft_disabled": dict(controls.get("soft_disabled", {}) or {}),
        "manual_disabled": dict(controls.get("manual_disabled", {}) or {}),
        "manual_enabled": dict(controls.get("manual_enabled", {}) or {}),
        "size_reductions": dict(controls.get("size_reductions", {}) or {}),
    }


def _plain_book_sentence(row: Dict) -> str:
    tag = str(row.get("strategy_tag", "unknown") or "unknown")
    status = str(row.get("status", "hold") or "hold")
    pnl = float(row.get("pnl", 0.0) or 0.0)
    expectancy = float(row.get("expectancy", 0.0) or 0.0)
    best_regime = (row.get("best_regime") or {}).get("name")
    worst_regime = (row.get("worst_regime") or {}).get("name")
    regime_text = ""
    if best_regime or worst_regime:
        regime_text = f" Best in {best_regime or 'n/a'}, weakest in {worst_regime or 'n/a'}."
    return (
        f"{tag} is {status}. "
        f"P&L ${pnl:.2f}, expectancy {expectancy:.2f}.{regime_text}"
    )


def _biggest_current_risk(book_rows: List[Dict], recon_summary: Dict, controls_summary: Dict) -> str:
    status = str(recon_summary.get("status", "unknown") or "unknown")
    trust = dict(recon_summary.get("trust_flags", {}) or {})
    if status == "critical_mismatch" or bool(trust.get("broker_only_mode")):
        return "Broker reconciliation is critical; capital protection and broker-truth mode should dominate."

    probation = controls_summary.get("probation", {}) or {}
    if probation:
        tags = ", ".join(sorted(probation.keys())[:3])
        return f"Books currently on probation need evidence or de-allocation: {tags}."

    lossy = [row for row in book_rows if float(row.get("expectancy", 0.0) or 0.0) < 0]
    lossy.sort(key=lambda row: float(row.get("pnl", 0.0) or 0.0))
    if lossy:
        row = lossy[0]
        return (
            f"{row.get('strategy_tag', 'unknown')} is the clearest current drag: "
            f"expectancy {float(row.get('expectancy', 0.0) or 0.0):.2f}, "
            f"pnl ${float(row.get('pnl', 0.0) or 0.0):.2f}."
        )

    overall = trade_history.get_analytics().get("overall", {}) or {}
    avg_win = float(overall.get("avg_win", 0.0) or 0.0)
    avg_loss = abs(float(overall.get("avg_loss", 0.0) or 0.0))
    if avg_loss > max(avg_win, 0.0):
        return (
            f"Loss asymmetry remains unresolved: average loss ${avg_loss:.2f} exceeds average win ${avg_win:.2f}."
        )

    return "Primary risk is still proof quality: allocator decisions need more multi-session evidence."


def build_governance_summary() -> Dict:
    analytics = trade_history.get_analytics()
    report = dict(analytics.get("book_report", {}) or {})
    book_rows = list(report.get("books", []) or [])
    recon_state = persistence.load_reconciliation_state() or {}
    controls = strategy_controls.load_controls()
    controls_meta = _controls_summary(controls)
    recon_meta = _reconciliation_summary(recon_state)
    change_ledger = persistence.load_change_ledger()
    recent_changes = sorted(
        list(change_ledger or []),
        key=lambda row: float(row.get("recorded_at", 0) or 0),
        reverse=True,
    )[:5]

    top_winners = _top_books(book_rows, positive=True, limit=3)
    top_losers = _top_books(book_rows, positive=False, limit=3)
    scaled_books = [row for row in book_rows if str(row.get("status", "")) == "scale"][:5]
    probation_books = [row for row in book_rows if str(row.get("status", "")) == "probation"][:5]
    disabled_books = [row for row in book_rows if str(row.get("status", "")) == "disable"][:5]

    return {
        "generated_at": time.time(),
        "book_report_summary": dict(report.get("summary", {}) or {}),
        "top_winning_books": top_winners,
        "top_losing_books": top_losers,
        "scaled_books": scaled_books,
        "probation_books": probation_books,
        "disabled_books": disabled_books,
        "reconciliation": recon_meta,
        "controls": controls_meta,
        "recent_changes": recent_changes,
        "biggest_current_risk": _biggest_current_risk(book_rows, recon_meta, controls_meta),
    }


def build_weekly_committee_memo() -> Dict:
    analytics = trade_history.get_analytics()
    summary = build_governance_summary()
    report = dict(analytics.get("book_report", {}) or {})
    books = list(report.get("books", []) or [])

    winning_sentences = [_plain_book_sentence(row) for row in summary.get("top_winning_books", [])]
    losing_sentences = [_plain_book_sentence(row) for row in summary.get("top_losing_books", [])]
    scaled_tags = [str(row.get("strategy_tag", "") or "") for row in summary.get("scaled_books", [])]
    probation_tags = [str(row.get("strategy_tag", "") or "") for row in summary.get("probation_books", [])]
    disabled_tags = [str(row.get("strategy_tag", "") or "") for row in summary.get("disabled_books", [])]

    recent_changes = []
    for row in summary.get("recent_changes", []) or []:
        title = str(row.get("title", row.get("change", "Unnamed change")) or "Unnamed change")
        mode = str(row.get("rollout_mode", row.get("mode", "unknown")) or "unknown")
        expected = str(row.get("expected_benefit", row.get("expected_upside", "")) or "").strip()
        recent_changes.append({
            "title": title,
            "rollout_mode": mode,
            "expected_benefit": expected,
        })

    memo = {
        "generated_at": time.time(),
        "executive_summary": {
            "total_trades": int(analytics.get("total_trades", 0) or 0),
            "total_pnl": float(analytics.get("total_pnl", 0.0) or 0.0),
            "win_rate_pct": float((analytics.get("overall", {}) or {}).get("win_rate_pct", 0.0) or 0.0),
            "top_line": (
                f"Velox has {int(analytics.get('total_trades', 0) or 0)} analytic trades, "
                f"net P&L ${float(analytics.get('total_pnl', 0.0) or 0.0):.2f}, "
                f"and {float((analytics.get('overall', {}) or {}).get('win_rate_pct', 0.0) or 0.0):.1f}% win rate."
            ),
            "biggest_current_risk": summary.get("biggest_current_risk", ""),
        },
        "what_worked": {
            "books": winning_sentences,
            "scale_candidates": scaled_tags,
        },
        "what_failed": {
            "books": losing_sentences,
            "probation_candidates": probation_tags,
            "disable_candidates": disabled_tags,
        },
        "book_statuses": {
            "scaled": scaled_tags,
            "probation": probation_tags,
            "disabled": disabled_tags,
            "books_seen": len(books),
        },
        "operational_reliability": {
            "reconciliation_status": summary.get("reconciliation", {}).get("status", "unknown"),
            "reconciliation_reasons": summary.get("reconciliation", {}).get("reasons", []),
            "critical_canaries": summary.get("reconciliation", {}).get("critical_canaries", []),
        },
        "recent_changes": recent_changes,
        "plain_english": {
            "worked": winning_sentences[:3],
            "failed": losing_sentences[:3],
            "biggest_risk": summary.get("biggest_current_risk", ""),
            "next_question": (
                "Which books deserve more or less capital based on verified expectancy, "
                "and where are oversized losers still breaking loss asymmetry?"
            ),
        },
    }

    memo["markdown"] = _render_weekly_committee_markdown(memo)
    return memo


def _render_weekly_committee_markdown(memo: Dict) -> str:
    executive = dict(memo.get("executive_summary", {}) or {})
    worked = list((memo.get("what_worked", {}) or {}).get("books", []) or [])
    failed = list((memo.get("what_failed", {}) or {}).get("books", []) or [])
    scale_candidates = list((memo.get("what_worked", {}) or {}).get("scale_candidates", []) or [])
    probation = list((memo.get("what_failed", {}) or {}).get("probation_candidates", []) or [])
    disabled = list((memo.get("what_failed", {}) or {}).get("disable_candidates", []) or [])
    ops = dict(memo.get("operational_reliability", {}) or {})
    changes = list(memo.get("recent_changes", []) or [])

    lines = [
        "# Velox Weekly Committee Memo",
        "",
        "## Executive Summary",
        f"- {executive.get('top_line', '')}",
        f"- Biggest current risk: {executive.get('biggest_current_risk', '')}",
        "",
        "## What Worked",
    ]
    lines.extend([f"- {row}" for row in worked] or ["- No clear winners yet."])
    lines.append("")
    lines.append("## What Failed")
    lines.extend([f"- {row}" for row in failed] or ["- No clear losing books yet."])
    lines.append("")
    lines.append("## Book Status Changes")
    lines.append(f"- Scaled candidates: {', '.join(scale_candidates) if scale_candidates else 'none'}")
    lines.append(f"- Probation candidates: {', '.join(probation) if probation else 'none'}")
    lines.append(f"- Disable candidates: {', '.join(disabled) if disabled else 'none'}")
    lines.append("")
    lines.append("## Operational Reliability")
    lines.append(f"- Reconciliation status: {ops.get('reconciliation_status', 'unknown')}")
    reasons = list(ops.get("reconciliation_reasons", []) or [])
    if reasons:
        lines.append(f"- Reconciliation reasons: {', '.join(str(r) for r in reasons)}")
    crit = list(ops.get("critical_canaries", []) or [])
    if crit:
        lines.append(f"- Critical canaries: {', '.join(str(row.get('code', 'unknown')) for row in crit[:3])}")
    lines.append("")
    lines.append("## Recent Changes")
    if changes:
        for row in changes:
            lines.append(
                f"- {row.get('title', 'Unnamed change')} "
                f"[{row.get('rollout_mode', 'unknown')}] "
                f"{('- ' + row.get('expected_benefit', '')) if row.get('expected_benefit') else ''}".rstrip()
            )
    else:
        lines.append("- No change ledger entries yet.")
    return "\n".join(lines).strip()
