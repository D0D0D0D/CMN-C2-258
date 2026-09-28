"""AgentCore Platform v1.0 — CMN-C2-258 inner step 1: CompetitorResearch."""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.web_research_service import WebResearchService


class CompetitorResearchNode(FunctionNode):
    """Inner-graph entry: unpack the outer JSON payload, collect public facts.

    The inner graph receives only user_input(str) + ctx (Cat-2 GraphNode
    contract), so this first node json.loads() the packed payload and expands
    competitors / deal_context / win_loss_records / missing_map into inner
    State. Web collection tolerates partial failure — zero collected pages is
    a valid outcome (internal-data-only report).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, web_research: WebResearchService | None = None) -> None:
        super().__init__()
        self._web = web_research or WebResearchService()

    def execute(self, state: dict) -> dict:
        try:
            payload = json.loads(state.get("user_input", "") or "{}")
        except (json.JSONDecodeError, ValueError):
            emit_trace_event("inner_payload_rejected", {"reason": "not valid JSON"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CompetitorResearchNode: inner payload is not valid JSON"],
            }

        competitors = payload.get("competitors") or []
        deal_context = payload.get("deal_context") or {}
        records = payload.get("win_loss_records") or []
        missing_map = payload.get("missing_map") or {}
        if not competitors:
            emit_trace_event("inner_payload_rejected", {"reason": "no competitors"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CompetitorResearchNode: no competitors in inner payload"],
            }

        url_map = deal_context.get("competitor_urls") or {}
        intel: list[dict] = []
        for competitor in competitors:
            urls = url_map.get(competitor) or []
            if not urls:
                intel.append(
                    {
                        "competitor": competitor,
                        "url": "",
                        "fetched_at": "",
                        "facts": [],
                        "error": "収集対象 URL 未指定（competitor_urls）",
                    }
                )
                continue
            intel.extend(self._web.research(competitor, urls))

        collected = sum(1 for e in intel if e.get("facts"))
        # S-4: audit the outbound collection (even when zero pages succeeded).
        emit_trace_event(
            "competitor_research_done",
            {
                "competitors": competitors,
                "pages_attempted": len(intel),
                "pages_with_facts": collected,
            },
            state,
        )
        return {
            "competitors": competitors,
            "deal_context": deal_context,
            "win_loss_records": records,
            "missing_map": missing_map,
            "competitor_intel": intel,
            "status": AgentStatus.SUCCESS.value,
        }
