"""Backtest result reporting."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


class ReportGenerator:
    """Generate JSON and markdown summaries from ranked results."""

    def generate(self, ranked_results: List[Dict], output_dir: str = "data/backtest_results") -> Dict[str, str]:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        json_path = out_dir / "results.json"
        report_path = out_dir / "REPORT.md"
        top_path = out_dir / "top_indicators.json"

        serializable = []
        for row in ranked_results or []:
            row_copy = dict(row)
            result = row_copy.get("result")
            if is_dataclass(result):
                row_copy["result"] = asdict(result)
            serializable.append(row_copy)

        json_path.write_text(json.dumps(serializable, indent=2, default=str))

        top_indicators = self._build_top_indicators(serializable)
        top_path.write_text(json.dumps(top_indicators, indent=2, default=str))
        report_path.write_text(self._build_markdown(serializable, top_indicators))

        return {
            "results_json": str(json_path),
            "report_md": str(report_path),
            "top_indicators_json": str(top_path),
        }

    def _build_top_indicators(self, ranked_results: List[Dict]) -> List[Dict]:
        unique = OrderedDict()
        for row in ranked_results:
            result = row.get("result", {})
            key = (
                result.get("indicator_name"),
                json.dumps(result.get("params", {}), sort_keys=True),
                result.get("side", "long"),
            )
            if key in unique:
                continue
            unique[key] = {
                "name": result.get("indicator_name"),
                "params": result.get("params", {}),
                "score": round(float(row.get("score", 0.0) or 0.0), 2),
                "win_rate": float(result.get("win_rate", 0.0) or 0.0),
                "sharpe": float(result.get("sharpe_ratio", 0.0) or 0.0),
                "profit_factor": float(result.get("profit_factor", 0.0) or 0.0),
                "side": result.get("side", "long"),
                "recommended_weight": round(min(1.0, max(0.25, float(row.get("score", 0.0) or 0.0) / 100.0)), 2),
            }
            if len(unique) >= 10:
                break
        return list(unique.values())

    def _build_markdown(self, ranked_results: List[Dict], top_indicators: List[Dict]) -> str:
        lines = [
            f"# Backtest Results — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            "",
            "## Top 10 Indicators by Composite Score",
            "",
            "| Rank | Indicator | Params | Score | Win Rate | Sharpe | PF | Max DD | Trades |",
            "|------|-----------|--------|-------|----------|--------|----|--------|--------|",
        ]
        for row in ranked_results[:10]:
            result = row.get("result", {})
            params = ", ".join(f"{k}={v}" for k, v in (result.get("params", {}) or {}).items()) or "-"
            lines.append(
                "| {rank} | {name} | {params} | {score:.1f} | {wr:.1%} | {sharpe:.2f} | {pf:.2f} | {dd:.1f}% | {trades} |".format(
                    rank=row.get("rank", "-"),
                    name=result.get("indicator_name", "?"),
                    params=params,
                    score=float(row.get("score", 0.0) or 0.0),
                    wr=float(result.get("win_rate", 0.0) or 0.0),
                    sharpe=float(result.get("sharpe_ratio", 0.0) or 0.0),
                    pf=float(result.get("profit_factor", 0.0) or 0.0),
                    dd=float(result.get("max_drawdown_pct", 0.0) or 0.0),
                    trades=int(result.get("total_trades", 0) or 0),
                )
            )

        lines.extend(
            [
                "",
                "## Edge Stability Analysis",
                "",
                "Strategies are ranked on risk-adjusted returns and stability across time halves.",
                "",
                "## Recommended Velox Integration",
                "",
            ]
        )
        for row in top_indicators:
            params = ", ".join(f"{k}={v}" for k, v in (row.get("params", {}) or {}).items()) or "-"
            lines.append(
                f"- {row.get('name')} ({params}) score={row.get('score'):.1f} weight={row.get('recommended_weight'):.2f}"
            )
        return "\n".join(lines) + "\n"

