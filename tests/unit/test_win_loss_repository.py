# CMN-C2-258 — Unit Tests: win_loss repository + Contract 1.0.0 validation

from __future__ import annotations

import datetime

from src.services.win_loss_repository import (
    WinLossRepository,
    apply_column_map,
    validate_records,
)


def _row(**over) -> dict:
    base = {
        "record_id": "r1",
        "deal_name": "案件A",
        "competitor": "CompX",
        "outcome": "won",
        "industry": "製造",
        "product_category": "ERP",
        "amount": 30_000_000,
        "close_date": "2026-01-15",
        "loss_reason": None,
    }
    base.update(over)
    return base


class TestColumnMap:
    def test_maps_physical_to_normalized(self):
        rows = [{"win_loss_id": "1", "rival": "CompX"}]
        mapped = apply_column_map(rows, {"record_id": "win_loss_id", "competitor": "rival"})
        assert mapped[0]["record_id"] == "1"
        assert mapped[0]["competitor"] == "CompX"

    def test_unknown_columns_pass_through(self):
        mapped = apply_column_map([{"foo": 1}], {"record_id": "id"})
        assert mapped[0]["foo"] == 1

    def test_empty_map_is_identity(self):
        rows = [{"record_id": "1"}]
        assert apply_column_map(rows, {}) == rows


class TestContractValidation:
    def test_valid_row_passes(self):
        records, missing = validate_records([_row()])
        assert len(records) == 1
        assert records[0]["outcome"] == "won"
        assert not missing

    def test_required_column_missing_quarantines(self):
        records, missing = validate_records([_row(record_id=None)])
        assert not records
        assert any("record_id" in r for rs in missing.values() for r in rs)

    def test_out_of_enum_outcome_quarantined_not_error(self):
        # Contract 1.0.0 §5: cancelled/postponed rows are permitted in the
        # table; the consumer quarantines them out of the win-rate count.
        records, missing = validate_records([_row(outcome="cancelled")])
        assert not records
        assert "r1" in missing

    def test_japanese_outcome_spellings_normalized(self):
        records, _ = validate_records([_row(outcome="受注"), _row(record_id="r2", outcome="失注")])
        assert [r["outcome"] for r in records] == ["won", "lost"]

    def test_unparseable_close_date_quarantined(self):
        records, missing = validate_records([_row(close_date="なし")])
        assert not records
        assert any("close_date" in r for r in missing["r1"])

    def test_datetime_close_date_serialized_to_iso_string(self):
        records, _ = validate_records([_row(close_date=datetime.datetime(2026, 2, 1, 12, 0))])
        assert records[0]["close_date"] == "2026-02-01"

    def test_non_numeric_amount_treated_as_missing(self):
        records, missing = validate_records([_row(amount="五千万")])
        assert len(records) == 1
        assert "amount" not in records[0]
        assert any("amount" in r for r in missing["r1"])

    def test_string_amount_normalized(self):
        records, _ = validate_records([_row(amount="50,000,000円")])
        assert records[0]["amount"] == 50_000_000

    def test_unknown_columns_dropped_from_state(self):
        # PII columns (email etc.) must never enter State — only contract
        # columns survive validation (forward compatibility + S-2).
        records, _ = validate_records([_row(customer_email="a@example.com")])
        assert "customer_email" not in records[0]

    def test_missing_optional_similarity_columns_tolerated(self):
        records, missing = validate_records([_row(industry=None, product_category=None)])
        assert len(records) == 1
        assert any("industry" in r for r in missing["r1"])


class TestRepositoryFetchBounds:
    def _repo(self, rows, **over):
        kwargs = {
            "rows": rows,
            "max_records": 3,
            "recent_years": 3,
            "today": lambda: datetime.date(2026, 7, 3),
        }
        kwargs.update(over)
        return WinLossRepository(**kwargs)

    def test_filters_by_competitor(self):
        rows = [_row(), _row(record_id="r2", competitor="Other")]
        records, _, _, _ = self._repo(rows).fetch(["CompX"])
        assert [r["record_id"] for r in records] == ["r1"]

    def test_filters_by_recent_years(self):
        rows = [_row(), _row(record_id="r2", close_date="2020-01-01")]
        records, _, _, _ = self._repo(rows).fetch(["CompX"])
        assert [r["record_id"] for r in records] == ["r1"]

    def test_truncation_flag_and_desc_order(self):
        rows = [
            _row(record_id=f"r{i}", close_date=f"2026-0{i}-01") for i in range(1, 6)
        ]
        records, _, truncated, raw = self._repo(rows).fetch(["CompX"])
        assert truncated is True
        assert len(records) == 3
        assert records[0]["close_date"] > records[-1]["close_date"]

    def test_no_source_returns_empty(self):
        records, missing, truncated, raw = WinLossRepository().fetch(["CompX"])
        assert records == [] and missing == {} and truncated is False and raw == 0
