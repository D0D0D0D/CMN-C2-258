"""AgentCore Platform v1.0 — CMN-C2-258 main slot (GraphNode wrapper)."""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.trust_level import TrustLevel


class CompetitiveAnalysisGraphNode(GraphNode):
    """`main` slot — wraps the inner CompetitiveAnalysisWorkflow.

    Cat-2 GraphNode contract (SDK 1.0.0): the inner graph receives only
    user_input(str) + ctx, so extract_input() JSON-packs the structured data
    and merge_output() maps the inner results back (changed keys only — full
    state return would double the reducer-managed histories).

    S-2/S-3 here are the framework GraphNode deliberate no-op — gating is done
    by pre_process (entry) and post_process (exit) plus inner-node gates.
    Upstream status="error" passes through untouched (error_strategy applies
    to inner errors: propagate — DB/LLM failures are never swallowed).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False  # no HITL in this template

    def __init__(self, parent_config: dict | None = None) -> None:
        super().__init__()
        self._injected = parent_config or {}
        self._subgraph = None

    def get_subgraph(self):
        # Cache the inner graph — one instance per node lifetime.
        if self._subgraph is None:
            from src.graph.domain_workflow_graph import CompetitiveAnalysisWorkflow

            self._subgraph = CompetitiveAnalysisWorkflow(config=self._parent_config())
        return self._subgraph

    def extract_input(self, state: AgentState) -> str:
        """JSON-pack the pre_process outputs into the inner user_input."""
        return json.dumps(
            {
                "competitors": state.get("competitors", []),
                "deal_context": state.get("deal_context", {}),
                "win_loss_records": state.get("win_loss_records", []),
                "missing_map": state.get("missing_map", {}),
            },
            ensure_ascii=False,
            default=str,  # close_date etc. — dates serialize safely
        )

    def merge_output(self, state: AgentState, sub_result: dict) -> dict:
        return {
            "competitor_intel": sub_result.get("competitor_intel", []),
            "win_rate_stats": sub_result.get("win_rate_stats", {}),
            "loss_reasons": sub_result.get("loss_reasons", {}),
            "synthesis": sub_result.get("synthesis", {}),
            "status": sub_result.get("status"),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> dict:
        """Forward injected dependencies (llm / fetcher / tuning) to the inner graph."""
        return {
            "llm": self._injected.get("llm"),
            "fetcher": self._injected.get("fetcher"),
            "web_research": self._injected.get("web_research"),
            "amount_bands": self._injected.get("amount_bands"),
            "fallback_order": self._injected.get("fallback_order"),
            "min_sample": self._injected.get("min_sample"),
            "web_rate_limit_sec": self._injected.get("web_rate_limit_sec"),
            "web_timeout_sec": self._injected.get("web_timeout_sec"),
            "web_max_pages_per_competitor": self._injected.get("web_max_pages_per_competitor"),
            "fact_max_chars": self._injected.get("fact_max_chars"),
        }
