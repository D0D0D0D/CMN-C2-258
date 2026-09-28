"""Shared fixtures for live LLM smoke coverage.

The values mirror `tests/integration/test_end_to_end.py`: the same win/loss
rows, fake public-web fetcher, and request payload are used so the live check
does not introduce a test-only happy path.
"""

from __future__ import annotations

import pytest


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
    {"record_id": "r4", "competitor": "CompX", "outcome": "cancelled", "close_date": "2026-04-01"},
]


def _fake_fetch(url: str, timeout: float):  # noqa: ARG001
    if url.endswith("robots.txt"):
        return 200, "User-agent: *\nAllow: /"
    return 200, (
        "<html><head><title>CompX ERP Suite</title>"
        '<meta name="description" content="CompX の ERP は保守込み価格。"></head>'
        "<body><h1>機能一覧</h1></body></html>"
    )


@pytest.fixture
def agent_config(monkeypatch) -> dict:
    """Graph config for `work/scripts/mrb_live_llm_check.py`.

    Matches the integration `_graph()` wiring: Contract rows are supplied via
    `win_loss_rows`, public-web collection uses the fake fetcher, and DNS/IP
    blocking is disabled by the same monkeypatch as `offline_dns`.
    """
    import src.services.web_research_service as wrs

    monkeypatch.setattr(wrs, "_is_blocked_ip", lambda host: False)
    return {"win_loss_rows": _ROWS, "fetcher": _fake_fetch, "web_rate_limit_sec": 0.0}


@pytest.fixture
def case_payload() -> dict:
    """Integration `_PAYLOAD` before JSON serialization."""
    return {
        "competitors": ["CompX"],
        "industry": "製造",
        "product_category": "ERP",
        "amount": 45_000_000,
        "competitor_urls": {"CompX": ["https://example.com/product"]},
    }


@pytest.fixture
def live_input_context() -> dict:
    """The agent payload carries the business input; no extra context is needed."""
    return {"mode": "invoke"}
