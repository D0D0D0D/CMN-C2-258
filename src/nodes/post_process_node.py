"""AgentCore Platform v1.0 — CMN-C2-258 post_process (outer backbone exit)."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.services import report as report_service

_QUOTE_MAX_CHARS = 90


def _mask_pii(text: str) -> tuple[str, int]:
    """Mask PII spans in the final report (web content may carry third-party PII)."""
    hits = detect_pii(text)
    if not hits:
        return text, 0
    for h in sorted(hits, key=lambda x: x.get("start", 0), reverse=True):
        start, end = h.get("start"), h.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
            text = text[:start] + f"[{h.get('type', 'pii')}-redacted]" + text[end:]
    return text, len(hits)


class PostProcessNode(FunctionNode):
    """Render the final Markdown report; S-3 output gate before delivery.

    S-3 domain checks (on top of the framework @final credential scan):
      (a) PII scrub of the final report
      (b) verbatim-quote clip: no collected web fact reproduced beyond
          _QUOTE_MAX_CHARS (著作権法47条の5 軽微利用 guard)
      (c) sample-annotation verify: every N% carries its sample count —
          a violating rate line is redacted rather than delivered
    The builder itself never reads raw win_loss records (aggregates only),
    so per-deal confidential data cannot be enumerated by construction.
    `synthesis` is OPTIONAL input — when the LLM was unavailable the report
    ships the deterministic sections and says so explicitly.
    """

    # S-1 trust gate: outer backbone exit — deliver only to a verified caller.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, *, quote_max_chars: int = _QUOTE_MAX_CHARS) -> None:
        super().__init__()
        self._quote_max_chars = quote_max_chars
        self._collected_facts: list[str] = []

    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 domain checks (b)(c) + PII scrub on the outgoing report."""
        for key in ("output", "formatted_output", "result"):
            val = result.get(key)
            if not isinstance(val, str) or not val:
                continue
            val = report_service.clip_long_quotes(
                val, self._collected_facts, quote_max_chars=self._quote_max_chars
            )
            violations = report_service.verify_sample_annotation(val)
            for line in violations:
                val = val.replace(line, "（勝率表記をサンプル数注記の不備のため非表示化 — 監査ログ参照）")
            masked, _ = _mask_pii(val)
            result[key] = masked
        return result

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            # Preserve upstream error — do not fabricate output (S-4: audited).
            emit_trace_event(
                "report_skipped_upstream_error",
                {"error_log_tail": (state.get("error_log") or [])[-1:]},
                state,
            )
            return {"status": AgentStatus.ERROR.value}

        competitors = state.get("competitors") or []
        intel = state.get("competitor_intel") or []
        stats = state.get("win_rate_stats") or {}
        synthesis = state.get("synthesis") or {}
        deal_context = state.get("deal_context") or {}

        # Facts snapshot for the S-3 long-quote gate.
        self._collected_facts = [
            fact for entry in intel for fact in (entry.get("facts") or [])
        ]

        text = report_service.build_report(
            competitors=competitors,
            competitor_intel=intel,
            win_rate_stats=stats,
            synthesis=synthesis,
            missing_map=state.get("missing_map") or {},
            contract_alert=deal_context.get("contract_alert", ""),
        )
        emit_trace_event(
            "report_delivered",
            {
                "length": len(text),
                "synthesis_available": bool(synthesis.get("available")),
                "competitors": competitors,
            },
            state,
        )
        return {
            "formatted_output": text,
            "output": text,
            "result": text,
            "status": AgentStatus.SUCCESS.value,
        }
