# CMN-C2-258 — Unit Tests: outer/inner nodes

from __future__ import annotations

import datetime
import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.competitor_research_node import CompetitorResearchNode
from src.nodes.main_node import CompetitiveAnalysisGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.synthesis_node import SynthesisNode
from src.nodes.win_loss_aggregate_node import WinLossAggregateNode
from src.services.web_research_service import WebResearchService
from src.services.win_loss_repository import WinLossRepository


def _repo(rows):
    return WinLossRepository(rows=rows, today=lambda: datetime.date(2026, 7, 3))


_ROWS = [
    {
        "record_id": "r1",
        "competitor": "CompX",
        "outcome": "won",
        "industry": "製造",
        "product_category": "ERP",
        "amount": 30_000_000,
        "close_date": "2026-01-15",
    },
    {
        "record_id": "r2",
        "competitor": "CompX",
        "outcome": "lost",
        "industry": "製造",
        "product_category": "ERP",
        "amount": 40_000_000,
        "close_date": "2026-02-01",
        "loss_reason": "保守費用で負け 連絡先 test@example.com",
    },
]

_PAYLOAD = json.dumps(
    {"competitors": ["CompX"], "industry": "製造", "product_category": "ERP", "amount": 45_000_000}
)


class TestPreProcessNode:
    def test_declares_trust_level(self):
        assert PreProcessNode.required_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_empty_input_is_error(self):
        out = PreProcessNode(repository=_repo([])).execute({"user_input": "  "})
        assert out["status"] == AgentStatus.ERROR

    def test_plain_text_treated_as_competitor_name(self):
        out = PreProcessNode(repository=_repo(_ROWS)).execute({"user_input": "CompX"})
        assert out["status"] == AgentStatus.SUCCESS
        assert out["competitors"] == ["CompX"]

    def test_json_payload_parsed(self):
        out = PreProcessNode(repository=_repo(_ROWS)).execute({"user_input": _PAYLOAD})
        assert out["competitors"] == ["CompX"]
        assert out["deal_context"]["industry"] == "製造"
        assert out["deal_context"]["amount"] == 45_000_000
        assert len(out["win_loss_records"]) == 2

    def test_no_competitor_is_error(self):
        out = PreProcessNode(repository=_repo(_ROWS)).execute(
            {"user_input": json.dumps({"industry": "製造"})}
        )
        assert out["status"] == AgentStatus.ERROR

    def test_no_rows_at_all_is_error(self):
        # Contract-compliant data existence is the operating precondition
        # (public-info-only operation is out of scope by Proposal).
        out = PreProcessNode(repository=_repo([])).execute({"user_input": "CompX"})
        assert out["status"] == AgentStatus.ERROR
        assert any("Contract" in e for e in out["error_log"])

    def test_all_rows_quarantined_continues_with_contract_alert(self):
        # Route-(b) drift defence: rows exist but ALL violate the Contract —
        # NOT an error; a confirmation summary flows to the report instead.
        bad_rows = [{"record_id": "r1", "competitor": "CompX", "outcome": "保留", "close_date": "2026-01-01"}]
        out = PreProcessNode(repository=_repo(bad_rows)).execute({"user_input": "CompX"})
        assert out["status"] == AgentStatus.SUCCESS
        assert "Contract 非準拠" in out["deal_context"]["contract_alert"]
        assert out["win_loss_records"] == []
        assert out["missing_map"]

    def test_s4_emitted_on_error_paths(self, monkeypatch):
        # F-04/F-05: every execute() path — including error branches — must
        # emit at least one domain trace event.
        import src.nodes.pre_process_node as ppn

        events: list[str] = []
        monkeypatch.setattr(ppn, "emit_trace_event", lambda et, payload, state: events.append(et))

        node = PreProcessNode(repository=_repo(_ROWS))
        node.execute({"user_input": "   "})                      # empty input
        node.execute({"user_input": json.dumps({"industry": "x"})})  # no competitor

        class BrokenRepo:
            def fetch(self, competitors):
                raise RuntimeError("db down")

        PreProcessNode(repository=BrokenRepo()).execute({"user_input": "CompX"})  # db failure
        PreProcessNode(repository=_repo([])).execute({"user_input": "CompX"})     # data absent
        assert len(events) == 4

    def test_loss_reason_pii_masked_before_state(self):
        out = PreProcessNode(repository=_repo(_ROWS)).execute({"user_input": _PAYLOAD})
        reason = next(r for r in out["win_loss_records"] if r["record_id"] == "r2")["loss_reason"]
        assert "test@example.com" not in reason

    def test_s2_gate_bounds_input_size(self):
        node = PreProcessNode(repository=_repo(_ROWS))
        gated = node._extra_security_gate_input({"user_input": "x" * 20_001, "error_log": []})
        assert gated["status"] == AgentStatus.ERROR.value

    def test_db_failure_propagates_as_error(self):
        class BrokenRepo:
            def fetch(self, competitors):
                raise RuntimeError("db down")

        out = PreProcessNode(repository=BrokenRepo()).execute({"user_input": "CompX"})
        assert out["status"] == AgentStatus.ERROR
        assert any("db down" in e for e in out["error_log"])


def _web(monkeypatch):
    import src.services.web_research_service as wrs

    monkeypatch.setattr(wrs, "_is_blocked_ip", lambda host: False)

    def fetcher(url, timeout):
        if url.endswith("robots.txt"):
            return 200, "User-agent: *\nAllow: /"
        return 200, "<html><head><title>CompX Suite</title></head><body></body></html>"

    return WebResearchService(fetcher=fetcher, rate_limit_sec=0.0)


class TestCompetitorResearchNode:
    def test_invalid_json_payload_is_error(self, monkeypatch):
        out = CompetitorResearchNode(web_research=_web(monkeypatch)).execute(
            {"user_input": "not-json"}
        )
        assert out["status"] == AgentStatus.ERROR

    def test_expands_payload_and_collects(self, monkeypatch):
        payload = json.dumps(
            {
                "competitors": ["CompX"],
                "deal_context": {"competitor_urls": {"CompX": ["https://example.com/p"]}},
                "win_loss_records": [{"record_id": "r1"}],
                "missing_map": {},
            }
        )
        out = CompetitorResearchNode(web_research=_web(monkeypatch)).execute(
            {"user_input": payload}
        )
        assert out["status"] == AgentStatus.SUCCESS
        assert out["win_loss_records"] == [{"record_id": "r1"}]
        assert out["competitor_intel"][0]["facts"]

    def test_no_urls_yields_explicit_entry_not_failure(self, monkeypatch):
        payload = json.dumps(
            {"competitors": ["CompX"], "deal_context": {}, "win_loss_records": [], "missing_map": {}}
        )
        out = CompetitorResearchNode(web_research=_web(monkeypatch)).execute(
            {"user_input": payload}
        )
        assert out["status"] == AgentStatus.SUCCESS
        assert out["competitor_intel"][0]["error"].startswith("収集対象 URL 未指定")


class TestWinLossAggregateNode:
    def test_aggregates_from_state(self):
        out = WinLossAggregateNode().execute(
            {
                "win_loss_records": [
                    {"record_id": "r1", "competitor": "CompX", "outcome": "won", "close_date": "2026-01-01"},
                    {"record_id": "r2", "competitor": "CompX", "outcome": "lost", "close_date": "2026-02-01"},
                    {"record_id": "r3", "competitor": "CompX", "outcome": "lost", "close_date": "2026-03-01"},
                ],
                "competitors": ["CompX"],
                "deal_context": {},
            }
        )
        assert out["status"] == AgentStatus.SUCCESS
        assert out["win_rate_stats"]["CompX"]["total"] == 3
        assert out["win_rate_stats"]["CompX"]["win_rate_pct"] == 33


class _FakeLLM:
    def __init__(self, content=None, error=None):
        self._content = content
        self._error = error

    def complete(self, messages):
        if self._error:
            raise self._error
        return {"content": self._content}


class TestSynthesisNode:
    def test_no_llm_degrades_gracefully(self):
        out = SynthesisNode(llm=None).execute({"win_rate_stats": {}, "competitor_intel": []})
        assert out["status"] == AgentStatus.SUCCESS
        assert out["synthesis"]["available"] is False

    def test_llm_success_parsed(self):
        content = json.dumps(
            {"comparison_table_md": "|a|", "differentiators": ["d1"], "loss_patterns": ["p1"]}
        )
        out = SynthesisNode(llm=_FakeLLM(content=content)).execute(
            {"win_rate_stats": {}, "competitor_intel": [], "loss_reasons": {}}
        )
        assert out["synthesis"]["available"] is True
        assert out["synthesis"]["differentiators"] == ["d1"]

    def test_llm_failure_degrades_not_error(self):
        out = SynthesisNode(llm=_FakeLLM(error=RuntimeError("timeout"))).execute(
            {"win_rate_stats": {}, "competitor_intel": [], "loss_reasons": {}}
        )
        assert out["status"] == AgentStatus.SUCCESS
        assert out["synthesis"]["available"] is False
        assert "timeout" in out["synthesis"]["reason"]

    def test_fenced_json_parsed(self):
        content = '```json\n{"comparison_table_md": "", "differentiators": [], "loss_patterns": []}\n```'
        out = SynthesisNode(llm=_FakeLLM(content=content)).execute(
            {"win_rate_stats": {}, "competitor_intel": [], "loss_reasons": {}}
        )
        assert out["synthesis"]["available"] is True


_STATS = {
    "CompX": {
        "wins": 1,
        "total": 3,
        "win_rate_pct": 33,
        "confidence": "ok",
        "applied_filters": ["competitor"],
        "relaxed_filters": [],
        "truncated": False,
        "note": "",
    }
}


class TestPostProcessNode:
    def test_upstream_error_passes_through(self):
        out = PostProcessNode().execute({"status": AgentStatus.ERROR.value})
        assert out == {"status": AgentStatus.ERROR}

    def test_builds_report_with_sample_annotation(self):
        out = PostProcessNode().execute(
            {
                "competitors": ["CompX"],
                "competitor_intel": [],
                "win_rate_stats": _STATS,
                "synthesis": {},
            }
        )
        assert out["status"] == AgentStatus.SUCCESS
        assert "33%（3件中1勝）" in out["output"]
        assert "LLM 統合分析を省略" in out["output"]

    def test_s3_redacts_percentage_without_sample(self):
        node = PostProcessNode()
        result = node._extra_security_gate_output({"output": "- CompX: 勝率 90%"})
        assert "90%" not in result["output"]

    def test_s3_clips_long_verbatim_fact(self):
        node = PostProcessNode(quote_max_chars=10)
        node._collected_facts = ["0123456789ABCDEF"]
        result = node._extra_security_gate_output({"output": "quote: 0123456789ABCDEF 件中"})
        assert "0123456789ABCDEF" not in result["output"]

    def test_s3_masks_pii(self):
        node = PostProcessNode()
        result = node._extra_security_gate_output({"output": "contact test@example.com 件中"})
        assert "test@example.com" not in result["output"]


class TestGraphNodeContract:
    def test_extract_input_json_packs_state(self):
        node = CompetitiveAnalysisGraphNode()
        packed = node.extract_input(
            {
                "competitors": ["CompX"],
                "deal_context": {"amount": 1},
                "win_loss_records": [{"close_date": datetime.date(2026, 1, 1)}],
                "missing_map": {},
            }
        )
        data = json.loads(packed)
        assert data["competitors"] == ["CompX"]
        assert data["win_loss_records"][0]["close_date"] == "2026-01-01"  # default=str

    def test_merge_output_returns_changed_keys_only(self):
        node = CompetitiveAnalysisGraphNode()
        merged = node.merge_output(
            {}, {"win_rate_stats": {"CompX": {}}, "status": "success", "node_history": ["x"]}
        )
        assert "node_history" not in merged  # reducer-managed — never returned
        assert merged["win_rate_stats"] == {"CompX": {}}

    def test_subgraph_cached(self):
        node = CompetitiveAnalysisGraphNode(parent_config={})
        assert node.get_subgraph() is node.get_subgraph()
