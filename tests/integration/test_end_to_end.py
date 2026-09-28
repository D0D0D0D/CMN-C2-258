# CMN-C2-258 — Integration Tests: outer backbone end-to-end (offline fakes)

from __future__ import annotations

import json

import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import CompetitiveAnalysisWinRateEstimationGraph

_ROWS = [
    {
        "record_id": "r1",
        "deal_name": "A社案件",
        "competitor": "CompX",
        "outcome": "won",
        "industry": "製造",
        "product_category": "ERP",
        "amount": 50_000_000,
        "close_date": "2026-01-10",
    },
    {
        "record_id": "r2",
        "competitor": "CompX",
        "outcome": "lost",
        "industry": "製造",
        "product_category": "ERP",
        "amount": 30_000_000,
        "close_date": "2026-02-02",
        "loss_reason": "保守費用で負けた",
    },
    {
        "record_id": "r3",
        "competitor": "CompX",
        "outcome": "lost",
        "industry": "製造",
        "product_category": "ERP",
        "amount": 45_000_000,
        "close_date": "2026-03-01",
        "loss_reason": "価格",
    },
    # out-of-enum outcome — permitted in the table, quarantined by validation
    {"record_id": "r4", "competitor": "CompX", "outcome": "cancelled", "close_date": "2026-04-01"},
]

_PAYLOAD = json.dumps(
    {
        "competitors": ["CompX"],
        "industry": "製造",
        "product_category": "ERP",
        "amount": 45_000_000,
        "competitor_urls": {"CompX": ["https://example.com/product"]},
    }
)


def _fake_fetch(url: str, timeout: float):
    if url.endswith("robots.txt"):
        return 200, "User-agent: *\nAllow: /"
    return 200, (
        "<html><head><title>CompX ERP Suite</title>"
        '<meta name="description" content="CompX の ERP は保守込み価格。"></head>'
        "<body><h1>機能一覧</h1></body></html>"
    )


class _FakeLLM:
    def complete(self, messages):
        return {
            "content": json.dumps(
                {
                    "comparison_table_md": "| 項目 | CompX |\n|---|---|\n| 保守 | 込み |",
                    "differentiators": ["保守費用の訴求を強化"],
                    "loss_patterns": ["保守費用で負けるパターン"],
                },
                ensure_ascii=False,
            )
        }


@pytest.fixture()
def offline_dns(monkeypatch):
    import src.services.web_research_service as wrs

    monkeypatch.setattr(wrs, "_is_blocked_ip", lambda host: False)


def _graph(**config_over):
    config = {
        "win_loss_rows": _ROWS,
        "fetcher": _fake_fetch,
        "llm": _FakeLLM(),
        "web_rate_limit_sec": 0.0,
    }
    config.update(config_over)
    g = CompetitiveAnalysisWinRateEstimationGraph(config=config)
    g.compile()
    return g


def _ctx(session: str) -> InvocationContext:
    return InvocationContext(
        session_id=session,
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        caller_id="integration-test",
    )


class TestEndToEnd:
    def test_success_path_full_report(self, offline_dns):
        out = _graph().invoke(_PAYLOAD, ctx=_ctx("e2e-1"))
        assert str(out.get("status")).endswith("SUCCESS") or out.get("status") == "success"
        report = out.get("output", "")
        # deterministic rate with mandatory sample annotation
        assert "3件中1勝" in report
        # web facts with source
        assert "https://example.com/product" in report
        # LLM synthesis present
        assert "差別化ポイント" in report
        # raw records / deal names never enumerated (S-3 by construction)
        assert "A社案件" not in report

    def test_backbone_traverses_all_five_nodes(self, offline_dns):
        out = _graph().invoke(_PAYLOAD, ctx=_ctx("e2e-2"))
        history = out.get("node_history", [])
        assert history[0] == "InitializeNode"
        assert "PreProcessNode" in history
        assert "CompetitiveAnalysisGraphNode" in history
        assert "PostProcessNode" in history
        assert history[-1] == "FinalizeNode"

    def test_no_llm_degrades_to_deterministic_report(self, offline_dns):
        out = _graph(llm=None).invoke(_PAYLOAD, ctx=_ctx("e2e-3"))
        report = out.get("output", "")
        assert "3件中1勝" in report
        assert "LLM 統合分析を省略" in report

    def test_no_win_loss_data_is_error_not_public_info_report(self, offline_dns):
        # Contract-compliant data existence is the operating precondition.
        out = _graph(win_loss_rows=[]).invoke(_PAYLOAD, ctx=_ctx("e2e-4"))
        assert str(out.get("status")).endswith("ERROR") or out.get("status") == "error"

    def test_all_rows_non_compliant_delivers_confirmation_summary(self, offline_dns):
        # Route-(b) drift defence: rows exist but ALL violate the Contract —
        # NOT an error; the report carries a Contract confirmation alert.
        bad_rows = [
            {"record_id": "b1", "competitor": "CompX", "outcome": "保留", "close_date": "2026-01-01"},
            {"record_id": "b2", "competitor": "CompX", "outcome": "won", "close_date": "不明"},
        ]
        out = _graph(win_loss_rows=bad_rows).invoke(_PAYLOAD, ctx=_ctx("e2e-6"))
        report = out.get("output", "")
        assert "Contract 準拠確認" in report
        assert "隔離した行" in report  # data-quality note (F-03)

    def test_web_collection_failure_still_delivers_internal_report(self, offline_dns):
        def broken_fetch(url, timeout):
            raise OSError("network down")

        out = _graph(fetcher=broken_fetch).invoke(_PAYLOAD, ctx=_ctx("e2e-5"))
        report = out.get("output", "")
        assert "3件中1勝" in report
        assert "取得失敗" in report
