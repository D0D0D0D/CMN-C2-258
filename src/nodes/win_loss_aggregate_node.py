"""AgentCore Platform v1.0 — CMN-C2-258 inner step 2: WinLossAggregate."""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.aggregate import (
    DEFAULT_AMOUNT_BANDS,
    DEFAULT_FALLBACK_ORDER,
    MIN_SAMPLE_FULL_CONFIDENCE,
    aggregate_win_rates,
)


class WinLossAggregateNode(FunctionNode):
    """Deterministic win-rate aggregation — no LLM anywhere near the numbers.

    Similarity filter + staged fallback + mandatory sample counts. Band edges
    and fallback order are config (the Data Contract deliberately does not fix
    amount bands — Contract 1.0.0 R-1 resolution).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        *,
        amount_bands=DEFAULT_AMOUNT_BANDS,
        fallback_order=DEFAULT_FALLBACK_ORDER,
        min_sample: int = MIN_SAMPLE_FULL_CONFIDENCE,
    ) -> None:
        super().__init__()
        self._bands = tuple(amount_bands)
        self._fallback = tuple(fallback_order)
        self._min_sample = min_sample

    def execute(self, state: dict) -> dict:
        records = state.get("win_loss_records") or []
        competitors = state.get("competitors") or []
        deal_context = state.get("deal_context") or {}

        stats, loss_reasons = aggregate_win_rates(
            records,
            competitors,
            deal_context,
            amount_bands=self._bands,
            fallback_order=self._fallback,
            min_sample=self._min_sample,
            truncated=bool(deal_context.get("truncated_fetch")),
        )
        # S-4: audit the deterministic aggregation (auditable numbers).
        emit_trace_event(
            "win_rate_aggregated",
            {
                competitor: {"total": s["total"], "wins": s["wins"], "relaxed": s["relaxed_filters"]}
                for competitor, s in stats.items()
            },
            state,
        )
        return {
            "win_rate_stats": stats,
            "loss_reasons": loss_reasons,
            "status": AgentStatus.SUCCESS.value,
        }
