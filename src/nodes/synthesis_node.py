"""AgentCore Platform v1.0 — CMN-C2-258 inner step 3: Synthesis (LLM)."""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.services.llm.base_llm import BaseLLM
from shared.utils.audit_logger import emit_trace_event

_PROMPT = """あなたは B2B 営業の競合分析アシスタントです。以下の材料から競合比較の統合分析を JSON で返してください。

## 材料
### 競合公開情報（Web 収集の事実スニペット・出典つき）
{intel}

### 過去戦績（決定論集計済み — 数値は変更しないこと）
{stats}

### 失注理由の自由記述（社内・要約対象）
{loss_reasons}

## 出力（JSON のみ・コードフェンス不要）
{{"comparison_table_md": "自社観点を含まない競合機能・価格の比較表（Markdown）",
  "differentiators": ["差別化ポイント・訴求提案（各 1 文）"],
  "loss_patterns": ["失注理由の傾向（各 1 文・固有名詞や個人情報を含めない）"]}}

制約: 勝率などの数値を新たに作らない。公開情報は上記スニペットの範囲で言い換える（長文引用しない）。
社外秘（案件名・顧客名・金額）を出力しない。"""


class SynthesisNode(FunctionNode):
    """LLM synthesis: comparison table + differentiators + loss patterns.

    The LLM is injected (BaseLLM) so tests run with a fake. LLM failure or
    absence degrades gracefully: synthesis is marked unavailable and the
    pipeline continues with the deterministic sections only (docs/02_design.md
    — post_process treats synthesis as optional input).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: BaseLLM | None = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        stats = state.get("win_rate_stats") or {}
        intel = state.get("competitor_intel") or []
        loss_reasons = state.get("loss_reasons") or {}

        if self._llm is None:
            emit_trace_event("synthesis_skipped", {"reason": "llm not configured"}, state)
            return {
                "synthesis": {"available": False, "reason": "LLM 未設定"},
                "status": AgentStatus.SUCCESS.value,
            }

        prompt = _PROMPT.format(
            intel=json.dumps(
                [{k: e.get(k) for k in ("competitor", "url", "facts")} for e in intel],
                ensure_ascii=False,
            ),
            stats=json.dumps(stats, ensure_ascii=False),
            loss_reasons=json.dumps(loss_reasons, ensure_ascii=False),
        )
        try:
            resp = self._llm.complete([{"role": "user", "content": prompt}])
            parsed = self._parse(resp.get("content", ""))
        except Exception as exc:  # noqa: BLE001 — degrade, don't fail the pipeline
            emit_trace_event("synthesis_failed", {"error": str(exc)}, state)
            return {
                "synthesis": {"available": False, "reason": f"LLM 呼び出し失敗: {exc}"},
                "status": AgentStatus.SUCCESS.value,
            }

        emit_trace_event(
            "synthesis_generated",
            {
                "differentiators": len(parsed.get("differentiators", [])),
                "loss_patterns": len(parsed.get("loss_patterns", [])),
            },
            state,
        )
        return {"synthesis": {"available": True, **parsed}, "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _parse(content: str) -> dict:
        text = str(content).strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):] if "{" in text else text
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("synthesis LLM output is not a JSON object")
        return {
            "comparison_table_md": str(data.get("comparison_table_md", "")),
            "differentiators": [str(d) for d in data.get("differentiators", []) if str(d).strip()],
            "loss_patterns": [str(p) for p in data.get("loss_patterns", []) if str(p).strip()],
        }
