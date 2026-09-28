"""AgentCore Platform v1.0 — CMN-C2-258 pre_process (outer backbone entry)."""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Read input_context via state.get("input_context", {}) — read-only [C1]

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event

from src.services.win_loss_repository import WinLossRepository

# S-2: bound raw user_input size before any DB fetch / inner dispatch.
_MAX_INPUT_LEN = 20_000


def _mask_pii_text(text: str) -> str:
    """Mask PII spans inside a free-text value (loss_reason etc.)."""
    hits = detect_pii(text)
    for h in sorted(hits, key=lambda x: x.get("start", 0), reverse=True):
        start, end = h.get("start"), h.get("end")
        if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text):
            text = text[:start] + f"[{h.get('type', 'pii')}-redacted]" + text[end:]
    return text


class PreProcessNode(FunctionNode):
    """Extract competitors + deal context; fetch Contract-validated win_loss.

    Cat-2 outer backbone entry: first security layer before the inner domain
    graph. Declares S-1 trust level; adds an S-2 size bound on top of the
    framework PII scan; masks PII in loss_reason free text before it enters
    State (and therefore before it can reach the LLM).
    """

    # S-1 trust gate: verified caller required before shared-DB access and
    # web/LLM dispatch. The declaration is mandatory: the trust gate runs in
    # __call__() before execute(), and an undeclared node defaults to ANONYMOUS.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, repository: WinLossRepository | None = None) -> None:
        super().__init__()
        self._repository = repository or WinLossRepository()

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 domain check: bound raw user_input size (must not raise)."""
        user_input = state.get("user_input", "")
        if isinstance(user_input, str) and len(user_input) > _MAX_INPUT_LEN:
            return {
                **state,
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    *state.get("error_log", []),
                    f"PreProcessNode: user_input exceeds {_MAX_INPUT_LEN} chars",
                ],
            }
        return state

    def execute(self, state: dict) -> dict:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only [C1]

        if not user_input or not str(user_input).strip():
            emit_trace_event("input_rejected", {"reason": "empty user_input"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        competitors, deal_context = self._parse_request(user_input, input_context)
        if not competitors:
            emit_trace_event("input_rejected", {"reason": "no competitor specified"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    "PreProcessNode: no competitor specified "
                    '(expected JSON payload {"competitors": [...]} or a plain competitor name)'
                ],
            }

        try:
            records, missing_map, truncated, raw_count = self._repository.fetch(competitors)
        except Exception as exc:  # noqa: BLE001 — DB failure propagates as error status
            emit_trace_event("win_loss_fetch_failed", {"error": str(exc)}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: win_loss fetch failed: {exc}"],
            }

        # Mask PII inside retained free text BEFORE it enters State / the LLM.
        for record in records:
            if record.get("loss_reason"):
                record["loss_reason"] = _mask_pii_text(record["loss_reason"])

        if not records and raw_count == 0:
            # Contract-required data ABSENT — out of scope by Proposal
            # (public-info-only operation would dissolve the moat).
            emit_trace_event(
                "win_loss_data_absent", {"competitors": competitors, "raw_count": 0}, state
            )
            return {
                "status": AgentStatus.ERROR.value,
                "missing_map": missing_map,
                "error_log": [
                    "PreProcessNode: no win_loss rows for "
                    f"{competitors} — Contract 準拠データの存在が本 AG の稼働前提です",
                ],
            }

        if not records:
            # Rows exist but ALL violate the Contract — route-(b) drift defence:
            # NOT an error. Continue with a confirmation summary so the operator
            # can fix the schema (column_map) — docs/02_design.md.
            deal_context["contract_alert"] = (
                f"取得 {raw_count} 行すべてが Contract 非準拠として隔離されました。"
                "スキーマ（列名ズレは column_map）を確認してください。"
            )

        deal_context["truncated_fetch"] = truncated
        emit_trace_event(
            "win_loss_fetched",
            {
                "competitors": competitors,
                "records": len(records),
                "quarantined": len(missing_map),
                "truncated": truncated,
            },
            state,
        )
        return {
            "validated_input": str(user_input).strip(),
            "competitors": competitors,
            "deal_context": deal_context,
            "win_loss_records": records,
            "missing_map": missing_map,
            "status": AgentStatus.SUCCESS.value,
        }

    def _parse_request(self, user_input, input_context: dict) -> tuple[list[str], dict]:
        """Parse a JSON payload (preferred) or plain competitor-name text.

        JSON payload: {"competitors": [...], "industry": ..., "product_category": ...,
                       "amount": 50000000, "competitor_urls": {name: [url, ...]}}
        Plain text:   treated as a single competitor name (no deal context).
        """
        competitors: list[str] = []
        deal_context: dict = {}
        raw = str(user_input).strip()
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                payload = {}
            if isinstance(payload, dict):
                comp = payload.get("competitors") or payload.get("competitor")
                if isinstance(comp, str):
                    competitors = [comp.strip()] if comp.strip() else []
                elif isinstance(comp, list):
                    competitors = [str(c).strip() for c in comp if str(c).strip()]
                for key in ("industry", "product_category"):
                    if isinstance(payload.get(key), str) and payload[key].strip():
                        deal_context[key] = payload[key].strip()
                if isinstance(payload.get("amount"), (int, float)):
                    deal_context["amount"] = int(payload["amount"])
                if isinstance(payload.get("competitor_urls"), dict):
                    deal_context["competitor_urls"] = {
                        str(k): [str(u) for u in v]
                        for k, v in payload["competitor_urls"].items()
                        if isinstance(v, list)
                    }
        else:
            competitors = [raw]

        # competitor_urls may also arrive via input_context (read-only)
        if "competitor_urls" not in deal_context and isinstance(
            input_context.get("competitor_urls"), dict
        ):
            deal_context["competitor_urls"] = {
                str(k): [str(u) for u in v]
                for k, v in input_context["competitor_urls"].items()
                if isinstance(v, list)
            }
        return competitors, deal_context
