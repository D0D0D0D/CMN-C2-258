"""AgentCore Platform v1.0 — CMN-C2-258 state schema."""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Enterprise Competitive Analysis & Win Rate Estimation state.

    Flat TypedDict (primitives + JSON-serializable only). All shared fields
    (user_input, status, session_id, node_history, error_log, etc.) are
    inherited from AgentState. Fields below are domain-specific.

    Outer backbone:  pre_process -> main(GraphNode) -> post_process
    Inner workflow:  CompetitorResearch -> WinLossAggregate -> Synthesis
    """

    # pre_process: competitor product names extracted from user_input
    competitors: list

    # pre_process: current deal context used by the similarity filter
    # {industry, product_category, amount(int|None), competitor_urls{name:[url]}}
    deal_context: dict

    # pre_process: Contract-validated win_loss rows (column_map applied,
    # PII dropped, primitives only — dates serialized to ISO strings)
    win_loss_records: list

    # pre_process/aggregate: record_id -> quarantine/missing reasons
    missing_map: dict

    # inner CompetitorResearch: collected public facts per competitor
    # [{competitor, url, fetched_at, facts: [str], error?}]
    competitor_intel: list

    # inner WinLossAggregate: per-competitor deterministic win-rate stats
    # {competitor: {wins, total, win_rate_pct|None, confidence, applied_filters,
    #               relaxed_filters, truncated, note}}
    win_rate_stats: dict

    # inner WinLossAggregate: per-competitor loss_reason free texts for LLM
    loss_reasons: dict

    # inner Synthesis ({"available": False, ...} when LLM unavailable/failed)
    # {available, comparison_table_md, differentiators, loss_patterns, reason?}
    synthesis: dict

    # post_process: final Markdown competitive report
    output: str
