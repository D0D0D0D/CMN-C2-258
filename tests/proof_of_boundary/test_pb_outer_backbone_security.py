# PB (outer backbone): Cat-2 outer PreProcess/PostProcess security gates
#
# For a Cat-2 template the OUTER backbone nodes are the first and last
# security layer around the inner graph.
# This proof-of-boundary suite verifies the outer nodes actually enforce:
#
#   PreProcessNode  — S-1 required_trust_level=VERIFIED_EXTERNAL,
#                     S-2 _extra_security_gate_input() bounds raw user_input,
#                     PII masking of win_loss free text BEFORE it enters State.
#   PostProcessNode — S-1 required_trust_level=VERIFIED_EXTERNAL,
#                     S-3 _extra_security_gate_output(): PII scrub +
#                     verbatim-quote clip + sample-annotation verify.
#   GraphNode(main) — S-1 declared; S-2/S-3 deliberate no-op per ADR-017.

from __future__ import annotations

import datetime

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.main_node import CompetitiveAnalysisGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import _MAX_INPUT_LEN, PreProcessNode
from src.services.win_loss_repository import WinLossRepository


def _val(v):
    return v.value if hasattr(v, "value") else v


class TestOuterBackboneTrustLevel:
    def test_outer_nodes_require_verified_external(self):
        # S-1: entry/exit nodes must not stay at the permissive ANONYMOUS
        # default — they gate the inner graph (shared DB + web + LLM).
        assert PreProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL
        assert PostProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL
        assert CompetitiveAnalysisGraphNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


class TestPreProcessInputGate:
    def test_oversized_input_rejected_before_execute(self):
        # S-2 hook contract: flag with status=error, never raise.
        node = PreProcessNode(repository=WinLossRepository())
        out = node._extra_security_gate_input({"user_input": "x" * (_MAX_INPUT_LEN + 1)})
        assert _val(out.get("status")) == "error"
        assert any("exceeds" in e for e in out.get("error_log", []))

    def test_normal_input_passes_gate_unchanged(self):
        node = PreProcessNode(repository=WinLossRepository())
        out = node._extra_security_gate_input({"user_input": "CompX"})
        assert _val(out.get("status")) != "error"

    def test_loss_reason_pii_never_enters_state(self):
        rows = [
            {
                "record_id": "r1",
                "competitor": "CompX",
                "outcome": "lost",
                "close_date": "2026-01-01",
                "loss_reason": "担当 jane@example.com に負けた",
            }
        ]
        repo = WinLossRepository(rows=rows, today=lambda: datetime.date(2026, 7, 3))
        out = PreProcessNode(repository=repo).execute({"user_input": "CompX"})
        assert "jane@example.com" not in out["win_loss_records"][0]["loss_reason"]


class TestPostProcessOutputGate:
    def test_pii_is_masked_in_final_output(self):
        report = "出典コメント: contact jane@example.com（3件中1勝）"
        out = PostProcessNode()._extra_security_gate_output({"output": report})
        assert "jane@example.com" not in out["output"]
        assert "redacted" in out["output"]

    def test_percentage_without_sample_is_redacted(self):
        # S-3(c): every rate figure must carry its sample size — a violating
        # line is redacted rather than delivered.
        out = PostProcessNode()._extra_security_gate_output({"output": "- CompX: 勝率 90%"})
        assert "90%" not in out["output"]

    def test_long_verbatim_web_fact_is_clipped(self):
        # S-3(b): 著作権法47条の5 軽微利用 guard.
        node = PostProcessNode(quote_max_chars=10)
        node._collected_facts = ["ABCDEFGHIJKLMNOP"]
        out = node._extra_security_gate_output({"output": "引用: ABCDEFGHIJKLMNOP（3件中）"})
        assert "ABCDEFGHIJKLMNOP" not in out["output"]

    def test_execute_preserves_upstream_error(self):
        out = PostProcessNode().execute({"status": AgentStatus.ERROR.value})
        assert _val(out["status"]) == "error"


class TestGraphNodeGates:
    def test_graph_node_s2_s3_are_deliberate_noop(self):
        # ADR-017: GraphNode S-2/S-3 are deliberate no-ops — gating is done by
        # the outer entry/exit nodes and the inner nodes' own gates.
        node = CompetitiveAnalysisGraphNode()
        state = {"user_input": "x"}
        assert node._security_gate_input(dict(state)) == state
        result = {"output": "y"}
        assert node._security_gate_output(dict(result)) == result
