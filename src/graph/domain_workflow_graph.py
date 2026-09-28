"""AgentCore Platform v1.0 — CMN-C2-258 inner CompetitiveAnalysisWorkflow.

Cat 2 inner graph (BaseGraph, custom linear topology). Instantiated by
CompetitiveAnalysisGraphNode.get_subgraph() in src/nodes/main_node.py.

Pipeline:
    START -> competitor_research -> win_loss_aggregate -> synthesis -> END

Dependency injection: config may carry `llm` (BaseLLM), `fetcher`
(network seam) or a full `web_research` (WebResearchService), plus
aggregation tuning (amount_bands / fallback_order / min_sample). All are
optional so unit/integration tests run offline; no LLM degrades to the
deterministic sections only.
"""

from __future__ import annotations

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph

from src.nodes.competitor_research_node import CompetitorResearchNode
from src.nodes.synthesis_node import SynthesisNode
from src.nodes.win_loss_aggregate_node import WinLossAggregateNode
from src.schemas.state import State
from src.services.aggregate import (
    DEFAULT_AMOUNT_BANDS,
    DEFAULT_FALLBACK_ORDER,
    MIN_SAMPLE_FULL_CONFIDENCE,
)
from src.services.web_research_service import WebResearchService


class CompetitiveAnalysisWorkflow(BaseGraph):
    """Inner multi-step workflow: Web research -> deterministic stats -> LLM."""

    @property
    def name(self) -> str:
        return "CompetitiveAnalysisWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # All config keys are optional (llm/fetcher/tuning) — see module doc.
        pass

    def register_nodes(self) -> None:
        # No super() call — BaseGraph.register_nodes() is abstract.
        cfg = self.config
        web = cfg.get("web_research")
        if web is None:
            kwargs: dict = {"fetcher": cfg.get("fetcher")}
            if cfg.get("web_rate_limit_sec") is not None:
                kwargs["rate_limit_sec"] = cfg["web_rate_limit_sec"]
            if cfg.get("web_timeout_sec") is not None:
                kwargs["timeout_sec"] = cfg["web_timeout_sec"]
            if cfg.get("web_max_pages_per_competitor") is not None:
                kwargs["max_pages_per_competitor"] = cfg["web_max_pages_per_competitor"]
            if cfg.get("fact_max_chars") is not None:
                kwargs["fact_max_chars"] = cfg["fact_max_chars"]
            web = WebResearchService(**kwargs)

        self._nodes["competitor_research"] = CompetitorResearchNode(web_research=web)
        self._nodes["win_loss_aggregate"] = WinLossAggregateNode(
            amount_bands=cfg.get("amount_bands") or DEFAULT_AMOUNT_BANDS,
            fallback_order=cfg.get("fallback_order") or DEFAULT_FALLBACK_ORDER,
            min_sample=cfg.get("min_sample") or MIN_SAMPLE_FULL_CONFIDENCE,
        )
        self._nodes["synthesis"] = SynthesisNode(llm=cfg.get("llm"))

    def add_edges(self) -> None:
        self._sg.add_edge(START, "competitor_research")
        self._sg.add_edge("competitor_research", "win_loss_aggregate")
        self._sg.add_edge("win_loss_aggregate", "synthesis")
        self._sg.add_edge("synthesis", END)

    def route(self, state: State) -> str:  # noqa: ARG002 — required by BaseGraph ABC
        """Not wired — the inner topology is linear (no conditional edges).

        BaseGraph declares route() abstract, so a concrete implementation is
        required. Annotated with the template's own State (never AgentState)
        per the Cat-2 conditional-route contract, in case a future revision
        wires it into add_conditional_edges().
        """
        return END

    def get_output(self, state) -> dict:
        return {
            "competitor_intel": state.get("competitor_intel", []),
            "win_rate_stats": state.get("win_rate_stats", {}),
            "loss_reasons": state.get("loss_reasons", {}),
            "synthesis": state.get("synthesis", {}),
            "status": state.get("status"),
            "error_log": state.get("error_log", []),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
