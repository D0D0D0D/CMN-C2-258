"""AgentCore Platform v1.0 — CMN-C2-258 outer graph (Cat 2).

Cat 2: AgentBaseGraph fixed 5-node backbone with a GraphNode in the `main`
slot wrapping the inner CompetitiveAnalysisWorkflow
(src/graph/domain_workflow_graph.py).

    START → initialize → pre_process → main → {route} → post_process → finalize → END

Domain complexity lives inside CompetitiveAnalysisGraphNode.get_subgraph().
Dependency injection (win_loss repository / llm / fetcher / tuning) flows
from this graph's config into the nodes so unit and integration tests run
offline. add_edges()/route() are NOT overridden — backbone wiring belongs to
the framework.
"""

from __future__ import annotations

from typing import ClassVar

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.schemas.trust_level import TrustLevel

from src.nodes.main_node import CompetitiveAnalysisGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.win_loss_repository import WinLossRepository


class CompetitiveAnalysisWinRateEstimationGraph(AgentBaseGraph):
    """Enterprise Competitive Analysis & Win Rate Estimation Agent (Cat 2).

    Exported class name matches config/agent.yaml `class:`.
    """

    # S-1 trust gate: declared in the class body so the framework enforces it
    # (config/agent.yaml is registry metadata only — not read by the S-1 gate).
    # VERIFIED_EXTERNAL = minimum for standard business operations
    # (shared-DB read + outbound web collection + LLM).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    @property
    def name(self) -> str:
        return "Enterprise Competitive Analysis & Win Rate Estimation Agent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode

        cfg = self.config
        repository = cfg.get("win_loss_repository")
        if repository is None:
            repository = WinLossRepository(
                db=cfg.get("db"),
                rows=cfg.get("win_loss_rows"),
                table=cfg.get("win_loss_table", "win_loss"),
                column_map=cfg.get("column_map") or {},
                max_records=cfg.get("max_records", 500),
                recent_years=cfg.get("recent_years", 3),
            )

        self._nodes["pre_process"] = PreProcessNode(repository=repository)
        self._nodes["main"] = CompetitiveAnalysisGraphNode(parent_config=cfg)
        self._nodes["post_process"] = PostProcessNode(
            quote_max_chars=cfg.get("quote_max_chars", 90)
        )

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Backwards-compatible alias (scaffold tooling imports `Graph`).
Graph = CompetitiveAnalysisWinRateEstimationGraph
