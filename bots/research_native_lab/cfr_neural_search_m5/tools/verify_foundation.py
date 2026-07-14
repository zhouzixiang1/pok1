"""Verify the additive M5 foundation without producing labels or training."""

from __future__ import annotations

import json

from ..core.contracts import (
    foundation_contract_digest,
    load_cfv_semantics,
    load_oracle_gate_contract,
    verify_m4_dependency,
)
from ..core.independence import verify_import_independence


def verify_foundation() -> dict[str, object]:
    m4 = verify_m4_dependency()
    semantics = load_cfv_semantics()
    oracle_gate = load_oracle_gate_contract()
    independence = verify_import_independence()
    return {
        "schema": "route-b-m5-foundation-gate-v1",
        "status": "passed_no_labels_no_training",
        "foundation_contract_sha256": foundation_contract_digest(),
        "m4": m4,
        "cfv_semantics_schema": semantics["schema"],
        "private_combo_count": semantics["private_combo_index"]["combo_count"],
        "oracle_gate_schema": oracle_gate["schema"],
        "independence": independence,
    }


def main() -> int:
    print(json.dumps(verify_foundation(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
