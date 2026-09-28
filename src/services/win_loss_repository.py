"""AgentCore Platform v1.0 — CMN-C2-258 win_loss repository + Contract validation.

Reads the shared sales-support DB `win_loss` table (Data Contract master:
Enterprise Sales Support Data Foundation Builder Agent, contract_version 1.0.0) and validates
each row against the contract before it enters State.

Injection seams:
  - `db`: shared.services.database BaseDatabase (fetch_many) — production
  - `rows`: pre-materialized rows — tests / inline payloads (offline)

Contract validation (route-(b) drift defence — docs/02_design.md):
  (1) required columns present (record_id / competitor / outcome / close_date)
  (2) outcome normalizable to won/lost (out-of-enum outcomes such as
      cancelled/postponed are PERMITTED in the table but quarantined here —
      Contract 1.0.0 §5)
  (3) close_date parseable as ISO date
  (4) amount numeric when present (non-numeric -> treated as missing)
Violating rows are quarantined into missing_map with reasons — never an error.
"""

from __future__ import annotations

import datetime as _dt
from typing import Callable

# Normalized column names required by the similarity/aggregation pipeline.
REQUIRED_COLUMNS = ("record_id", "competitor", "outcome", "close_date")
OPTIONAL_COLUMNS = ("deal_name", "industry", "product_category", "amount", "loss_reason")

# outcome spellings normalized to the Contract enum (won / lost)
_OUTCOME_MAP = {
    "won": "won", "win": "won", "勝": "won", "勝ち": "won", "受注": "won",
    "lost": "lost", "lose": "lost", "loss": "lost", "負": "lost", "負け": "lost", "失注": "lost",
}


def apply_column_map(rows: list[dict], column_map: dict) -> list[dict]:
    """Map physical column names to normalized names ({normalized: physical}).

    Unknown physical columns are carried through untouched (and later ignored
    — forward compatibility per Contract §7).
    """
    if not column_map:
        return rows
    inverse = {v: k for k, v in column_map.items()}
    return [{inverse.get(k, k): v for k, v in row.items()} for row in rows]


def _parse_date(value) -> str | None:
    """Return ISO date string or None when unparseable."""
    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        try:
            return _dt.date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            return None
    return None


def _parse_amount(value) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        digits = value.replace(",", "").replace("円", "").strip()
        try:
            amount = int(float(digits))
        except ValueError:
            return None
        return amount if amount >= 0 else None
    return None


def validate_records(rows: list[dict]) -> tuple[list[dict], dict]:
    """Contract-validate rows; return (valid_records, missing_map).

    Valid records contain only contract columns (unknown columns dropped =
    PII never enters State), primitives only, close_date as ISO string.
    """
    valid: list[dict] = []
    missing: dict = {}
    for idx, row in enumerate(rows):
        record_id = row.get("record_id")
        key = str(record_id) if record_id not in (None, "") else f"row#{idx}"
        reasons: list[str] = []

        if record_id in (None, ""):
            reasons.append("record_id missing")

        competitor = row.get("competitor")
        if not isinstance(competitor, str) or not competitor.strip():
            reasons.append("competitor missing")

        outcome = _OUTCOME_MAP.get(str(row.get("outcome", "")).strip().lower())
        if outcome is None:
            # Contract 1.0.0 §5: out-of-enum outcomes are permitted in the
            # table; consumers quarantine them out of the win-rate count.
            reasons.append(f"outcome not normalizable to won/lost: {row.get('outcome')!r}")

        close_date = _parse_date(row.get("close_date"))
        if close_date is None:
            reasons.append("close_date missing or unparseable")

        if reasons:
            missing[key] = reasons
            continue

        record = {
            "record_id": str(record_id),
            "competitor": competitor.strip(),
            "outcome": outcome,
            "close_date": close_date,
        }
        # Optional similarity/analysis columns (missing tolerated; noted).
        lacking: list[str] = []
        for col in ("industry", "product_category"):
            val = row.get(col)
            if isinstance(val, str) and val.strip():
                record[col] = val.strip()
            else:
                lacking.append(col)
        amount = _parse_amount(row.get("amount"))
        if amount is not None:
            record["amount"] = amount
        elif row.get("amount") is not None:
            lacking.append("amount not numeric")
        else:
            lacking.append("amount")
        loss_reason = row.get("loss_reason")
        if isinstance(loss_reason, str) and loss_reason.strip():
            record["loss_reason"] = loss_reason.strip()
        deal_name = row.get("deal_name")
        if isinstance(deal_name, str) and deal_name.strip():
            # Internal trace only — the report builder never reads this field.
            record["deal_name"] = deal_name.strip()
        if lacking:
            missing[key] = [f"optional column missing: {c}" for c in lacking]
        valid.append(record)
    return valid, missing


class WinLossRepository:
    """Bounded, read-only access to the Contract 1.0.0 `win_loss` table."""

    def __init__(
        self,
        db=None,
        rows: list[dict] | None = None,
        *,
        table: str = "win_loss",
        column_map: dict | None = None,
        max_records: int = 500,
        recent_years: int = 3,
        today: Callable[[], _dt.date] | None = None,
    ) -> None:
        self._db = db
        self._rows = rows
        self._table = table
        self._column_map = column_map or {}
        self._max_records = max_records
        self._recent_years = recent_years
        self._today = today or _dt.date.today

    def fetch(self, competitors: list[str]) -> tuple[list[dict], dict, bool, int]:
        """Fetch Contract-validated records for the target competitors.

        Bounded at the query level (competitor IN + close_date >= N years ago
        + max_records, close_date desc) so State / the GraphNode JSON pack /
        checkpoints stay bounded.

        Returns (records, missing_map, truncated, raw_count). raw_count is the
        pre-validation row count — it lets the caller distinguish "no data at
        all" (out of scope -> error) from "rows exist but ALL violate the
        Contract" (confirmation summary, NOT an error — docs/02_design.md
        route-(b) drift defence).
        """
        since = (self._today() - _dt.timedelta(days=365 * self._recent_years)).isoformat()
        if self._rows is not None:
            raw = self._rows
        elif self._db is not None:
            placeholders = ", ".join(f"%(c{i})s" for i in range(len(competitors)))
            params = {f"c{i}": c for i, c in enumerate(competitors)}
            params["since"] = since
            physical = {**{c: c for c in REQUIRED_COLUMNS + OPTIONAL_COLUMNS}, **self._column_map}
            comp_col = physical.get("competitor", "competitor")
            date_col = physical.get("close_date", "close_date")
            query = (
                f"SELECT * FROM {self._table} "  # noqa: S608 — table name from config, not user input
                f"WHERE {comp_col} IN ({placeholders}) AND {date_col} >= %(since)s "
                f"ORDER BY {date_col} DESC LIMIT {int(self._max_records) + 1}"
            )
            raw = self._db.fetch_many(query, params)
        else:
            raw = []

        rows = apply_column_map(list(raw), self._column_map)
        records, missing = validate_records(rows)
        # In-memory re-application of the fetch bounds (rows seam / defensive)
        target = {c.strip() for c in competitors}
        records = [r for r in records if r["competitor"] in target and r["close_date"] >= since]
        records.sort(key=lambda r: r["close_date"], reverse=True)
        truncated = len(records) > self._max_records
        return records[: self._max_records], missing, truncated, len(rows)
