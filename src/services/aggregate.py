"""AgentCore Platform v1.0 — CMN-C2-258 deterministic win-rate aggregation.

Pure functions — no LLM, no I/O. Win rates are plain counts over
Contract-validated `win_loss` records so every number is reproducible and
auditable (docs/02_design.md Design Decision: LLM never touches the rate).

Similarity filter (docs/02_design.md WinLossAggregateNode):
    competitor match (always) AND industry match AND product_category match
    AND amount band proximity (same or adjacent band; bands come from
    config `amount_bands` — the Data Contract deliberately does not fix bands)

Staged fallback: when matches < min_sample, similarity filters are relaxed
one by one (config `fallback_order`, relaxed last-first) and every relaxation
is recorded in the stats (`relaxed_filters`) — never silent.
"""

from __future__ import annotations

import bisect

# Default band edges in JPY: <10M / 10-50M / 50-100M / >=100M
DEFAULT_AMOUNT_BANDS = (10_000_000, 50_000_000, 100_000_000)
DEFAULT_FALLBACK_ORDER = ("industry", "product_category", "amount_band")
MIN_SAMPLE_FULL_CONFIDENCE = 3


def amount_band(amount: int | None, bands: tuple[int, ...] | list[int]) -> int | None:
    """Return the 0-based band index for an amount (None when unknown)."""
    if amount is None:
        return None
    return bisect.bisect_right(sorted(bands), amount)


def _matches(record: dict, deal_context: dict, active_filters: set[str], bands) -> bool:
    """A record matches when every ACTIVE filter with data on both sides agrees.

    Missing-tolerance principle (docs/02_design.md / Contract §5): when either
    side lacks the column, that filter is skipped for the record instead of
    excluding it.
    """
    if "industry" in active_filters:
        want, have = deal_context.get("industry"), record.get("industry")
        if want and have and want.strip() != have.strip():
            return False
    if "product_category" in active_filters:
        want, have = deal_context.get("product_category"), record.get("product_category")
        if want and have and want.strip() != have.strip():
            return False
    if "amount_band" in active_filters:
        want = amount_band(deal_context.get("amount"), bands)
        have = amount_band(record.get("amount"), bands)
        if want is not None and have is not None and abs(want - have) > 1:
            return False
    return True


def aggregate_win_rates(
    records: list[dict],
    competitors: list[str],
    deal_context: dict,
    *,
    amount_bands=DEFAULT_AMOUNT_BANDS,
    fallback_order=DEFAULT_FALLBACK_ORDER,
    min_sample: int = MIN_SAMPLE_FULL_CONFIDENCE,
    truncated: bool = False,
) -> tuple[dict, dict]:
    """Aggregate per-competitor win-rate stats and loss-reason texts.

    Returns (win_rate_stats, loss_reasons). Every stat carries the sample
    size, the filters actually applied, any relaxation performed, and a
    confidence label — the report builder MUST surface these next to the
    percentage (S-3 gate re-verifies).
    """
    stats: dict = {}
    loss_reasons: dict = {}
    for competitor in competitors:
        pool = [r for r in records if r["competitor"] == competitor.strip()]

        active = list(fallback_order)
        relaxed: list[str] = []
        matched = [r for r in pool if _matches(r, deal_context, set(active), amount_bands)]
        # Staged fallback — relax similarity filters last-first, visibly.
        while len(matched) < min_sample and active:
            relaxed.append(active.pop())
            matched = [r for r in pool if _matches(r, deal_context, set(active), amount_bands)]

        wins = sum(1 for r in matched if r["outcome"] == "won")
        total = len(matched)
        rate = round(100 * wins / total) if total else None
        if total == 0:
            confidence, note = "no_data", "戦績データなし（勝率算出不可）"
        elif total < min_sample:
            confidence, note = "reference_only", f"参考値（サンプル僅少: {total}件）"
        else:
            confidence, note = "ok", ""
        stats[competitor] = {
            "wins": wins,
            "total": total,
            "win_rate_pct": rate,
            "confidence": confidence,
            "applied_filters": ["competitor", *active],
            "relaxed_filters": relaxed,
            "truncated": truncated,
            "note": note,
        }
        reasons = [r["loss_reason"] for r in matched if r["outcome"] == "lost" and r.get("loss_reason")]
        if reasons:
            loss_reasons[competitor] = reasons
    return stats, loss_reasons
