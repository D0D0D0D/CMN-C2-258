"""AgentCore Platform v1.0 — CMN-C2-258 Markdown report builder + S-3 checks.

build_report() renders the final competitive-analysis Markdown from
deterministic stats (+ optional LLM synthesis). It never touches raw
win_loss records — only aggregated values — so confidential per-deal data
(deal names, amounts, customer info) cannot leak into the report by
construction (S-3: no raw-record enumeration).

S-3 helper checks (called from PostProcessNode._extra_security_gate_output):
  - verify_sample_annotation(): every N% figure carries its sample size
  - clip_long_quotes(): no verbatim reproduction of collected web facts
    beyond `quote_max_chars` (著作権法47条の5 軽微利用 guard)
"""

from __future__ import annotations

import re

_DISCLAIMER = (
    "> ⚠️ 本レポートは営業担当のレビューを前提とした下書きです。Web 収集情報は出典・取得日時を確認の上、"
    "勝率は参考指標（過去戦績の件数カウント）としてご利用ください。"
)

_PCT_RE = re.compile(r"\d{1,3}\s*%")
_SAMPLE_NEARBY_RE = re.compile(r"(件中|N=|サンプル|勝率算出不可)")


def _rate_line(competitor: str, s: dict) -> str:
    """Render one win-rate line — the percentage ALWAYS carries its sample."""
    if s["win_rate_pct"] is None:
        return f"- **{competitor}**: 勝率算出不可（類似案件 0 件）"
    line = f"- **{competitor}**: 勝率 {s['win_rate_pct']}%（{s['total']}件中{s['wins']}勝）"
    if s["confidence"] == "reference_only":
        line += " ※参考値（サンプル僅少）"
    if s["relaxed_filters"]:
        line += f" ※類似条件を緩和済み: {', '.join(s['relaxed_filters'])} を除外"
    if s.get("truncated"):
        line += " ※対象期間内の全件ではない（取得上限で打ち切り）"
    return line


def build_report(
    *,
    competitors: list[str],
    competitor_intel: list[dict],
    win_rate_stats: dict,
    synthesis: dict | None,
    missing_map: dict | None = None,
    contract_alert: str = "",
) -> str:
    """Render the final Markdown report (aggregates only — no raw records)."""
    lines: list[str] = ["# 競合比較レポート", "", _DISCLAIMER, ""]

    if contract_alert:
        lines += [f"> 🛑 **Contract 準拠確認**: {contract_alert}", ""]

    lines += ["## 過去戦績ベースの勝率推定（決定論・件数カウント）", ""]
    for competitor in competitors:
        s = win_rate_stats.get(competitor)
        if s:
            lines.append(_rate_line(competitor, s))
    lines.append("")

    lines += ["## 競合公開情報（Web 収集・出典つき）", ""]
    any_intel = False
    for entry in competitor_intel:
        facts = entry.get("facts") or []
        if entry.get("error"):
            lines.append(f"- {entry['competitor']} — {entry['url']}: 取得失敗（{entry['error']}）")
            continue
        any_intel = True
        lines.append(f"- **{entry['competitor']}** — 出典: {entry['url']}（取得: {entry.get('fetched_at', '')}）")
        lines += [f"  - {fact}" for fact in facts]
    if not any_intel:
        lines.append("- 公開情報の収集結果なし（社内勝敗データのみのレポート）")
    lines.append("")

    # Data-quality note (F-03): surface quarantined rows / missing optional
    # columns so the reader can judge how much the similarity filter degraded.
    if missing_map:
        quarantined = sum(
            1 for reasons in missing_map.values() if any("optional" not in r for r in reasons)
        )
        degraded = len(missing_map) - quarantined
        lines += ["## データ品質ノート", ""]
        if quarantined:
            lines.append(
                f"- Contract 非準拠として勝率集計から隔離した行: {quarantined} 件（必須列欠損・enum 外の決着など）"
            )
        if degraded:
            lines.append(
                f"- 類似性フィルタ列（業種/商材/金額）に欠損がある行: {degraded} 件 — 該当列を除外して集計（フィルタ精度低下）"
            )
        lines.append("")

    if synthesis and synthesis.get("available"):
        lines += ["## 統合分析（LLM）", ""]
        if synthesis.get("comparison_table_md"):
            lines += [synthesis["comparison_table_md"], ""]
        if synthesis.get("differentiators"):
            lines += ["### 差別化ポイント", ""]
            lines += [f"- {d}" for d in synthesis["differentiators"]]
            lines.append("")
        if synthesis.get("loss_patterns"):
            lines += ["### 失注理由の傾向", ""]
            lines += [f"- {p}" for p in synthesis["loss_patterns"]]
            lines.append("")
    else:
        reason = (synthesis or {}).get("reason", "LLM 未設定")
        lines += ["## 統合分析（LLM）", "", f"- 本実行では LLM 統合分析を省略（{reason}）。上記の決定論集計をご利用ください。", ""]

    return "\n".join(lines)


# ---- S-3 helpers ----------------------------------------------------------

def verify_sample_annotation(text: str) -> list[str]:
    """Return violations: percentage figures lacking a nearby sample size."""
    violations = []
    for line in text.splitlines():
        if _PCT_RE.search(line) and not _SAMPLE_NEARBY_RE.search(line):
            violations.append(line.strip())
    return violations


def clip_long_quotes(text: str, facts: list[str], *, quote_max_chars: int = 90) -> str:
    """Clip any verbatim web-fact reproduction longer than quote_max_chars."""
    for fact in facts:
        if len(fact) > quote_max_chars and fact in text:
            text = text.replace(fact, fact[: quote_max_chars - 1] + "…")
    return text
