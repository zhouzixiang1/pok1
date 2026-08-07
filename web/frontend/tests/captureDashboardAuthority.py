"""Emit Dashboard P1 payloads through the real Python route builders.

This is a cross-language contract fixture, not a runtime snapshot.  It avoids
touching ``.evolution_pok`` while still proving that TypeScript accepts the
exact fields produced by the production Python normalization functions.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Opt into the active namespace the same way the runtime and conftest do:
# bot_namespace reads POK_CLOUD_RUNTIME at import time. Without this the cloud
# branch still resolves the main-namespace prefix and the fixture emits a
# non-canonical bot name. An explicit operator override still wins.
os.environ.setdefault("POK_CLOUD_RUNTIME", "1")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "web" / "core"))

from bot_namespace import FIRST_STRICT_POLICY_VERSION, bot_name  # noqa: E402
from server.routes import pipeline  # noqa: E402


IDENTITY = "a" * 64

# Branch-portable strict-generation identity. Derive the canonical active-
# namespace bot name (national_cloud_v1 on this branch) instead of hardcoding a
# main-branch literal (national_v143). The TS test asserts against this same
# derivation via the strict_identity echo below.
STRICT_TARGET_V = FIRST_STRICT_POLICY_VERSION
STRICT_SOURCE_V = FIRST_STRICT_POLICY_VERSION
STRICT_NEXT_V = FIRST_STRICT_POLICY_VERSION + 2
STRICT_TARGET_BOT = bot_name(STRICT_TARGET_V)


def main() -> None:
    gate_results = {}
    for name, allowed in pipeline._GATE_FIELD_ALLOWLIST.items():
        fields = {}
        for key in allowed:
            if key in {"attempt", "native_matches", "hands_per_match", "quality_score", "advisory_score"}:
                fields[key] = 1
            elif key.endswith("_digest") or key in {"code_fingerprint", "workflow_profile_digest"}:
                fields[key] = IDENTITY
            elif key in {"certification_profile", "opponent_authority"}:
                fields[key] = "fixture"
            else:
                fields[key] = True
        gate_results[name] = fields
    for helper in (
        "_quality_complete",
        "_review_complete",
        "_critic_advisory_complete",
        "_precommit_complete",
        "_official_full_complete",
    ):
        setattr(pipeline, helper, lambda _checkpoint: True)

    checkpoint = {
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "checkpoint_revision": 5,
        "next_v": STRICT_NEXT_V,
        "source_v": STRICT_SOURCE_V,
        "parent2_v": None,
        "stage": "official_certifying",
        "workflow_run_id": f"generation:{STRICT_NEXT_V}:fixture-v1",
        "run_id": f"{STRICT_NEXT_V}#0",
        "generation_attempt": 0,
        "audit_attempt": 1,
        "precommit_attempt": 0,
        "worker_failure_count": 1,
        "master_plan": {"analysis": "bound fixture plan", "tasks": []},
        "gate_results": gate_results,
        "infra_failure": {
            "schema_version": 1,
            "failure_class": "infrastructure",
            "component": "native_precommit",
            "code": "fixture_retry",
            "operation": "run_precommit_eval",
            "owner_tool": "run_precommit_eval",
            "resume_stage": "critic_checked",
            "attempt": 1,
            "max_attempts": 3,
            "reason": "bounded fixture",
            "retryable": True,
            "exhausted": False,
            "action": "retry_same_tool",
            "identity_digest": IDENTITY,
        },
    }
    agents = pipeline._build_agents_projection(
        checkpoint,
        [{
            "worker_id": 2,
            "role": "Policy Implementer",
            "error": "bounded fixture failure",
            "failure_type": "implementation",
            "category": "worker",
            "gen": 147,
            "timestamp": 1_784_400_000.0,
        }],
    )

    snapshot = {
        "available": False,
        "reason": "active_pool_singleton",
        "active_bots": [STRICT_TARGET_BOT],
        "epoch_reset_receipt_digest": IDENTITY,
        "evaluation_identity_digest": IDENTITY,
        "evaluation_manifest_digest": IDENTITY,
    }
    bundle = {
        "available": True,
        "active_bots": [STRICT_TARGET_BOT],
        "epoch_reset_receipt": {"receipt_digest": IDENTITY},
        "manifest": {"evaluation_identity_digest": IDENTITY},
        "manifest_digest": IDENTITY,
        "raw_files": {},
        "raw_append_logs": {"match_history": b""},
    }
    strength = pipeline._build_strength_jobs_projection(
        snapshot,
        bundle=bundle,
        offset=0,
        limit=50,
        budget=pipeline._StrengthObserverBudget(),
    )
    strength["daemon"] = {
        "alive": True,
        "configured": True,
        "heartbeat_status": "fresh",
        "pid": 12345,
    }

    print(json.dumps({
        "agents": agents,
        "strength": strength,
        # Branch-portable strict-generation identity echo so the cross-language
        # TS test can assert exact equality without hardcoding a namespace
        # prefix or version literal.
        "strict_identity": {
            "first_strict_policy_version": FIRST_STRICT_POLICY_VERSION,
            "strict_bot_name": STRICT_TARGET_BOT,
        },
    }, sort_keys=True))


if __name__ == "__main__":
    main()
