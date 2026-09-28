# CMN-C2-258 — Unit Tests: deterministic win-rate aggregation

from __future__ import annotations

from src.services.aggregate import aggregate_win_rates, amount_band


def _rec(**over) -> dict:
    base = {
        "record_id": "r1",
        "competitor": "CompX",
        "outcome": "won",
        "industry": "製造",
        "product_category": "ERP",
        "amount": 30_000_000,
        "close_date": "2026-01-01",
    }
    base.update(over)
    return base


_CTX = {"industry": "製造", "product_category": "ERP", "amount": 40_000_000}


class TestAmountBand:
    def test_bands(self):
        bands = (10_000_000, 50_000_000, 100_000_000)
        assert amount_band(5_000_000, bands) == 0
        assert amount_band(30_000_000, bands) == 1
        assert amount_band(70_000_000, bands) == 2
        assert amount_band(200_000_000, bands) == 3

    def test_none_amount(self):
        assert amount_band(None, (10,)) is None


class TestAggregate:
    def test_full_match_rate_with_sample(self):
        records = [
            _rec(record_id=f"r{i}", outcome="won" if i < 2 else "lost") for i in range(4)
        ]
        stats, _ = aggregate_win_rates(records, ["CompX"], _CTX)
        s = stats["CompX"]
        assert s["total"] == 4 and s["wins"] == 2 and s["win_rate_pct"] == 50
        assert s["confidence"] == "ok"
        assert s["relaxed_filters"] == []

    def test_adjacent_band_matches_but_far_band_does_not(self):
        near = _rec(record_id="near", amount=70_000_000)   # band 2 vs ctx band 1
        far = _rec(record_id="far", amount=200_000_000)    # band 3 vs ctx band 1
        stats, _ = aggregate_win_rates(
            [near, far, _rec(record_id="r3"), _rec(record_id="r4")], ["CompX"], _CTX
        )
        assert stats["CompX"]["total"] == 3  # far excluded

    def test_missing_similarity_column_tolerated(self):
        records = [_rec(record_id=f"r{i}", industry=None) for i in range(3)]
        records = [{k: v for k, v in r.items() if v is not None} for r in records]
        stats, _ = aggregate_win_rates(records, ["CompX"], _CTX)
        assert stats["CompX"]["total"] == 3  # industry filter skipped per record

    def test_staged_fallback_relaxes_and_records_it(self):
        # Only 1 full match; relaxing amount_band then product_category then
        # industry grows the pool. min_sample=3 forces relaxation.
        records = [
            _rec(record_id="full"),
            _rec(record_id="other_ind", industry="小売"),
            _rec(record_id="other_cat", product_category="CRM"),
        ]
        stats, _ = aggregate_win_rates(records, ["CompX"], _CTX)
        s = stats["CompX"]
        assert s["total"] == 3
        assert s["relaxed_filters"]  # relaxation is visible, never silent
        assert s["relaxed_filters"][0] == "amount_band"

    def test_small_sample_marked_reference_only(self):
        stats, _ = aggregate_win_rates([_rec()], ["CompX"], _CTX)
        s = stats["CompX"]
        assert s["confidence"] == "reference_only"
        assert "参考値" in s["note"]

    def test_no_data(self):
        stats, _ = aggregate_win_rates([], ["CompX"], _CTX)
        s = stats["CompX"]
        assert s["total"] == 0 and s["win_rate_pct"] is None
        assert s["confidence"] == "no_data"

    def test_loss_reasons_collected_for_lost_only(self):
        records = [
            _rec(record_id="w", outcome="won", loss_reason="should not appear"),
            _rec(record_id="l", outcome="lost", loss_reason="保守費用で負け"),
            _rec(record_id="l2", outcome="lost"),
        ]
        _, reasons = aggregate_win_rates(records, ["CompX"], _CTX)
        assert reasons == {"CompX": ["保守費用で負け"]}

    def test_truncated_flag_propagates(self):
        stats, _ = aggregate_win_rates(
            [_rec(record_id=f"r{i}") for i in range(3)], ["CompX"], _CTX, truncated=True
        )
        assert stats["CompX"]["truncated"] is True

    def test_multiple_competitors_isolated(self):
        records = [
            _rec(record_id="x1"),
            _rec(record_id="y1", competitor="CompY", outcome="lost"),
        ]
        stats, _ = aggregate_win_rates(records, ["CompX", "CompY"], _CTX)
        assert stats["CompX"]["wins"] == 1
        assert stats["CompY"]["wins"] == 0
