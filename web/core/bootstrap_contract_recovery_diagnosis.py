"""Diagnosis subsystem extracted from bootstrap_contract_recovery.py.

This companion houses the contract-failure diagnosis envelope validators and
the three exact-incident proof builders (legacy causal-order false-failure,
workflow-v64 called-all-in runout, workflow-v65 live-deferred THP-prefix).

Symbols are re-exported from the main module so existing callers
(``recovery._validate_contract_failure_diagnosis_envelope`` etc.) and tests
that monkeypatch ``recovery`` continue to work unchanged.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from bot_artifact import canonical_digest
import diagnosis_called_allin as _bcd
import diagnosis_v65 as _dv65  # noqa: E402,F401  (v65-diagnosis cluster)
from bot_namespace import (
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
    bot_tag,
    high_water_tag,
)

# Main-module helpers that are *not* monkeypatched by the test-suite: import
# once at top level. The two monkeypatched helpers (``_git`` and
# ``_read_regular_exact``) are intentionally NOT imported here; each diagnosis
# function that needs them performs a function-local re-import so the patch
# applied to ``bootstrap_contract_recovery`` stays effective.
from bootstrap_contract_recovery import (
    BootstrapContractRecoveryError,
    PARKED_EVALUATION_CONTRACT_VERSION,
    _HEX64,
    _git_absence,
    _legacy_owned_replay_projection,
    _legacy_replay_matches_stored,
    _legacy_wire_causalize,
    _regular_json,
    _require_exact_round_job_envelope,
    _require_regular_directory,
    _sha256_bytes,
    _strict_artifact_bytes,
    # Constants that stay in the main module because they are also referenced
    # by retained code (_terminal_job_recovery_profile, _legacy_wire_causalize,
    # build_claim, _validate_claim_envelope, _historical_terminal_job_matches).
    _CAUSAL_FAILURE_DIAGNOSIS_KIND,
    _CALLED_ALLIN_DIAGNOSIS_KIND,
    _CALLED_ALLIN_PROFILE_ID,
    _LEGACY_FALSE_WIRE_ISSUES,
    _LEGACY_STORED_REPLAY_FIELDS,
    _V65_BASELINE_CONTRACT_VERSION,
    _V65_DIAGNOSIS_KIND,
    _V65_PROFILE_ID,
    _V65_REPAIR_CONTRACT_VERSION,
)


_CAUSAL_FAILURE_DIAGNOSIS_FIELDS = frozenset({
    "schema_version",
    "kind",
    "defect_id",
    "baseline_wire_probe_sha256",
    "repair_wire_probe_sha256",
    "evidence_sha256",
    "evidence_archive_sha256",
    "evidence_archive_manifest_digest",
    "suite_summary_sha256",
    "attribution_digest",
    "original_issue_kinds",
    "original_issue_count",
    "rounds",
    "strength_evaluation",
    "disposition",
    "proof_digest",
})


_CAUSAL_FAILURE_DEFECT_ID = (
    "legacy-idle-flush-causal-order-false-positive-8-of-8-v1"
)


_CAUSAL_FAILURE_ROUND_FIELDS = frozenset({
    "slot",
    "round_id",
    "receipt_sha256",
    "wire_events_sha256",
    "replay_summary_sha256",
    "event_count",
    "stored_events_seen",
    "legacy_issue_kinds",
    "deferred_observation_bindings_digest",
    "legacy_summary_digest",
    "corrected_summary_digest",
    "max_pending_wait_sec",
    "corrected_hands_started",
    "corrected_settlements",
    "corrected_pending_count",
})


_LEGACY_DOWNSTREAM_FINDINGS = frozenset({
    "thp_missing_for_full_70_hand_round",
    "official_terminal_socket_boundary_invalid",
})


_LEGACY_INCIDENT_EVENT_COUNTS = (18, 24, 36, 20, 22, 24, 27, 21)


_LEGACY_INCIDENT_STORED_COUNTS = (17, 24, 35, 19, 21, 23, 27, 20)


_LEGACY_INCIDENT_HANDS = (1, 1, 2, 1, 1, 1, 1, 1)


_LEGACY_INCIDENT_SETTLEMENTS = (0, 0, 1, 0, 0, 0, 0, 0)


_CALLED_ALLIN_DEFECT_ID = (
    "official-2021-called-allin-wire-runout-omission-v1"
)


_CALLED_ALLIN_BASELINE_HEAD = (
    "8e3fa0a1c8d5455aaa3b8dc58cfb9a1e9ee8a7b5"
)


_CALLED_ALLIN_BASELINE_CONTRACT_HASH = (
    "ff176151c15a6ebcca8758cd37a3dbc4809673ccaa701c6cb7076fbb3c70c68d"
)


_CALLED_ALLIN_WORKFLOW_RUN_ID = "generation:143:workflow-v64"


_CALLED_ALLIN_CHECKPOINT_REVISION = 21


_CALLED_ALLIN_CANDIDATE_HASH = (
    "f4e7b845a9bc18827532208556b67b76c2ecbb63baf9d2cf8a2a65ef7a54ca50"
)


_CALLED_ALLIN_CONTROL_HASH = (
    "1cfe42b96566017ba470573b0aa9bc46a992c966779ff63db2470248d7440db2"
)


_CALLED_ALLIN_JOB_ID = (
    "37bc2c6555b516b6568f45c85cdf8b9e23b0c06e6bbca207d5367a561759dae6"
)


_CALLED_ALLIN_JOB_RESULT_DIGEST = (
    "c055966f5385fd921ece46920202a70477522d700bea106dd45cc1bae3196f9a"
)


_CALLED_ALLIN_EXE_SHA256 = (
    "9d01b443d4920a7e06a487d87ea1b050ea2ca5359023602f98c3c236c734e81a"
)


_CALLED_ALLIN_ORACLE_DOC = (
    "docs/official-allin-runout-wire-oracle-2026-07-19.md"
)


_CALLED_ALLIN_ORACLE_DOC_SHA256 = (
    "95b9945a106c8a20c688925d50448a7ddee7f34486fc4a79366a8b32c6cfdd2d"
)


_CALLED_ALLIN_ORACLE_FIXTURE = (
    "sever/tests/fixtures/official_allin_runout_wire_oracle_20260719.json"
)


_CALLED_ALLIN_ORACLE_FIXTURE_SHA256 = (
    "c17f9b1908031ce3d85abb5f995581a5a049449ed8c6bb94da0cf9c954646440"
)


_CALLED_ALLIN_EXPECTED_SLOTS = (
    "self_play_01",
    "self_play_02",
    "self_play_03",
    "self_play_04",
    "self_play_05",
    "opponent_01",
    "opponent_02",
    "opponent_03",
)


_CALLED_ALLIN_PASS_PATTERN = (
    False, False, True, True, True, False, True, True,
)


_CALLED_ALLIN_FALSE_FAILURES = (
    {
        "slot": "self_play_01",
        "hand": 4,
        "stage": "turn",
        "public_cards_observed": 4,
        "wire_events_sha256": (
            "fdf8356114caf957d2c871ed3c6273e4837d2f4c0b6982a4c6b52e4f0ea07e08"
        ),
        "record_seq": [145, 146, 148, 151, 152, 153, 154],
        "observation_seq": [93, 92, 94, 95, 96, 97, 98],
        "corrected_hands_started": 5,
        "corrected_settlements": 4,
    },
    {
        "slot": "self_play_02",
        "hand": 10,
        "stage": "flop",
        "public_cards_observed": 3,
        "wire_events_sha256": (
            "0d098d48fcb09b4a98de6c8742a3e53b4485461a404dafd7806785d814d0d77e"
        ),
        "record_seq": [268, 269, 271, 274, 275, 276, 277],
        "observation_seq": [173, 172, 174, 175, 176, 177, 178],
        "corrected_hands_started": 11,
        "corrected_settlements": 10,
    },
    {
        "slot": "opponent_01",
        "hand": 2,
        "stage": "preflop",
        "public_cards_observed": 0,
        "wire_events_sha256": (
            "5fd8a7dc035878fd2cd52c78048c59c0a264ead7bc615b22d82100cd2d5b11b7"
        ),
        "record_seq": [46, 47, 49, 52, 53, 54, 55],
        "observation_seq": [32, 31, 33, 34, 35, 36, 37],
        "corrected_hands_started": 3,
        "corrected_settlements": 2,
    },
)


_CALLED_ALLIN_DIAGNOSIS_FIELDS = frozenset({
    "schema_version",
    "kind",
    "profile_id",
    "defect_id",
    "incident_identity",
    "baseline_wire_probe_sha256",
    "repair_wire_probe_sha256",
    "baseline_harness_sha256",
    "repair_harness_sha256",
    "oracle_identity",
    "evidence_sha256",
    "evidence_archive_sha256",
    "evidence_archive_manifest_digest",
    "suite_summary_sha256",
    "attribution_digest",
    "round_receipts",
    "false_failures",
    "authority_absence",
    "strength_evaluation",
    "disposition",
    "proof_digest",
})


_CALLED_ALLIN_INCIDENT_IDENTITY_FIELDS = frozenset({
    "baseline_head",
    "baseline_contract_version",
    "baseline_contract_hash",
    "repair_contract_version",
    "workflow_run_id",
    "checkpoint_revision",
    "candidate_artifact_hash",
    "job_id",
    "job_result_digest",
    "rounds_requested",
    "rounds_completed",
    "rounds_run",
    "passed_rounds",
    "failed_rounds",
})


_CALLED_ALLIN_ORACLE_IDENTITY_FIELDS = frozenset({
    "document_path",
    "document_sha256",
    "fixture_path",
    "fixture_sha256",
    "oracle_id",
    "authority_scope",
    "strength_weight",
    "official_exe_sha256",
    "control_artifact_sha256",
    "observations_digest",
})


_CALLED_ALLIN_ROUND_RECEIPT_FIELDS = frozenset({
    "slot",
    "round_id",
    "passed",
    "receipt_sha256",
})


_CALLED_ALLIN_FALSE_FAILURE_FIELDS = frozenset({
    "slot",
    "round_id",
    "hand",
    "stage",
    "public_cards_observed",
    "receipt_sha256",
    "wire_events_sha256",
    "replay_summary_sha256",
    "event_count",
    "stored_summary_digest",
    "corrected_summary_digest",
    "omitted_runout_boundaries_digest",
    "corrected_hands_started",
    "corrected_settlements",
    "corrected_pending_count",
})


_CALLED_ALLIN_AUTHORITY_ABSENCE = {
    "certificate_present": False,
    "certificate_digest": None,
    "candidate_completed": False,
    "completion_tags": [],
    "active_bots": [],
    "strict_published_bots": [],
    "control_successful_count": 0,
    "control_max_successful_consumptions": 1,
}


_V65_DEFECT_IDS = (
    "causal-live-deferred-street-boundary-provisional-v1",
    "official-thp-called-allin-prefix-v1",
)


_V65_BASELINE_HEAD = (
    "3d3162844e42cae72905e15d2a297c0dd2b0e93a"
)


_V65_BASELINE_CONTRACT_HASH = (
    "d630466f867805abc7eaa0272e5d94f587caceca433ac617e8aae9643b08ce41"
)


_V65_WORKFLOW_RUN_ID = "generation:143:workflow-v65"


_V65_CHECKPOINT_REVISION = 21


_V65_CANDIDATE_HASH = (
    "f4e7b845a9bc18827532208556b67b76c2ecbb63baf9d2cf8a2a65ef7a54ca50"
)


_V65_CONTROL_HASH = (
    "1cfe42b96566017ba470573b0aa9bc46a992c966779ff63db2470248d7440db2"
)


_V65_JOB_ID = (
    "b4575bb7163f551cb586f6391f728c1e6dc1671b11a279a4392504af8a4c7ebf"
)


_V65_JOB_RESULT_DIGEST = (
    "fb7846b74c7c237226b99d2b4e8647c8b82ad9801917e59baceadd8d83424ce1"
)


_V65_BASELINE_WIRE_PROBE_SHA256 = (
    "19d1f9396cbd4df691f0b3c387dbf25d97e78bc422e7f4ed1fdb2e78bba36339"
)


_V65_BASELINE_HARNESS_SHA256 = (
    "0ef7a9baa1f77c8a305884ec3f00807f2fb71cbe65527bd206b8fc1e0fb97e94"
)


_V65_BASELINE_ORACLE_DOC_SHA256 = _CALLED_ALLIN_ORACLE_DOC_SHA256


_V65_BASELINE_ORACLE_FIXTURE_SHA256 = _CALLED_ALLIN_ORACLE_FIXTURE_SHA256


_V65_REPAIR_ORACLE_DOC_SHA256 = (
    "e6a0ef58656bd80ffdc2828a12920911e264a29de85ab61fb180332026dbb7e7"
)


_V65_REPAIR_ORACLE_FIXTURE_SHA256 = (
    "a81c804d1940437fb259d0119c7bc1b06e968fcd5f20eb4364ab3f594156ef48"
)


_V65_EXPECTED_SLOTS = _CALLED_ALLIN_EXPECTED_SLOTS


_V65_PASS_PATTERN = (
    True, False, False, True, False, False, False, False,
)


_V65_ROUND_IDENTITIES = (
    {
        "slot": "self_play_01",
        "round_id": "self_play_01_20260719_121934",
        "passed": True,
        "receipt_sha256": "81efa6f36dd5742023bfd36a4a270c0b856937354db6438f5fb4a09fd7ad878f",
        "wire_events_sha256": "932ae4dca9c77c3f3d19c24c63c1d97942471239c38dec235f476dbe02b614f0",
        "replay_summary_sha256": "ee8e0cc3f9aab3c5dfbc58c26f174c489875b7a08a6fb54d04e31fadc7a879b5",
        "event_count": 819,
        "hands_started": 70,
        "settlements": 69,
    },
    {
        "slot": "self_play_02",
        "round_id": "self_play_02_20260719_122513",
        "passed": False,
        "receipt_sha256": "65ee969fb4b2472a4b782377517f7cb85084dee8c138862a7766256740816a39",
        "wire_events_sha256": "ce317f8d80e285b4a617734f874bd1efb68b4155f297c9726fc3e1e036012c50",
        "replay_summary_sha256": "695ba6dd22450ebd2eccb552a2023a2ff86c70683671c82aa62e5c707126357a",
        "event_count": 18,
        "hands_started": 1,
        "settlements": 0,
    },
    {
        "slot": "self_play_03",
        "round_id": "self_play_03_20260719_122522",
        "passed": False,
        "receipt_sha256": "8537e4e05390a80c13e2dc6d5d06b7d94747da5e7997eb16bc47fa8631f81242",
        "wire_events_sha256": "e9882362fd1113ec4e5d219a41a364b14785332a1b63280ec853cbf8d689ad54",
        "replay_summary_sha256": "a60c4aa9cff006ce51299497a292ad8612a38c10c08f5c9edd9cdc4f4be901f0",
        "event_count": 865,
        "hands_started": 70,
        "settlements": 69,
    },
    {
        "slot": "self_play_04",
        "round_id": "self_play_04_20260719_123151",
        "passed": True,
        "receipt_sha256": "94eb8e2b9bd7f297344e75c19e187410f530e79a40268da10d408d7ddc736b0b",
        "wire_events_sha256": "3f87a3c4c326a9197945b8264fce96668b630b99d008b57c59bfccd36e146df9",
        "replay_summary_sha256": "efce92ce3090a092af2566cfc432beb4c1aeb362da78e2ccf06d101f88b143ad",
        "event_count": 828,
        "hands_started": 70,
        "settlements": 69,
    },
    {
        "slot": "self_play_05",
        "round_id": "self_play_05_20260719_123741",
        "passed": False,
        "receipt_sha256": "1143852bf3a4b2809d5dd9ccb57cb8f6bcff743d1db19846dee37cd1d22b6d68",
        "wire_events_sha256": "9b188502ec9c7e9952fa22f2cf4dfd2700f6eac5bdf1a5c5826a9c69ad504984",
        "replay_summary_sha256": "54f15ac3179e30ca49319aa24bf63dbe6aea9937de69b1be631fef8c6c79dd85",
        "event_count": 22,
        "hands_started": 1,
        "settlements": 0,
    },
    {
        "slot": "opponent_01",
        "round_id": "opponent_01_20260719_123754",
        "passed": False,
        "receipt_sha256": "c938be98346c94376a0b74febb4d23ec0e42bce79ad226441aabc4b9ab52efb5",
        "wire_events_sha256": "a9f2f11940313dff15e8a23328d2748a0a717e8e6da1542e6046a02958a4ee56",
        "replay_summary_sha256": "cc35ec881440c22c217b2db1fe0e27ebde247082689799c59efd8f9274889350",
        "event_count": 752,
        "hands_started": 70,
        "settlements": 69,
    },
    {
        "slot": "opponent_02",
        "round_id": "opponent_02_20260719_124241",
        "passed": False,
        "receipt_sha256": "49618fc54ee6259e7ded3b120cead85cefafa8a92d5fe2629c274d54c260a1ce",
        "wire_events_sha256": "791b554938cd1c31e9fdb76fe4a568157c47a7c41e3ee16efd7d3668cbcd72c8",
        "replay_summary_sha256": "b2fd38ab2f85f7d703603d7e2d68fc3a19b2c41184c3cce2eef4030290b234b7",
        "event_count": 60,
        "hands_started": 2,
        "settlements": 1,
    },
    {
        "slot": "opponent_03",
        "round_id": "opponent_03_20260719_124301",
        "passed": False,
        "receipt_sha256": "183ff21e8a794a5b38ab920d2ebb63d1d323563b258ac3adfac39d931c3b0a8a",
        "wire_events_sha256": "fa590230143b97b6f6074d6b18bdda7568336b82a95ef79cc4b6dfa02933a361",
        "replay_summary_sha256": "b84a4230953d36c583c6d0f47e458765ffc6a629ea2114c081d6833305d2c214",
        "event_count": 97,
        "hands_started": 3,
        "settlements": 2,
    },
)


_V65_LIVE_RACE_FAILURES = (
    {
        "slot": "self_play_02", "conn": "A", "hand": 1,
        "stage": "preflop", "action": "check",
        "source_record_seq": 12, "source_observation_seq": 9,
        "boundary_record_seq": 13, "boundary_observation_seq": 10,
        "boundary_message": "flop|<1,11><3,9><2,12>",
        "flush_record_seq": 15, "flush_observation_seq": 9,
        "stored_summary_digest": "fd0145f3afc5933339c4a300032ce39aee607fc8648ebe52239f284207a56391",
        "finalized_summary_digest": "fd0145f3afc5933339c4a300032ce39aee607fc8648ebe52239f284207a56391",
        "provisional_summary_digest": "4f84c16dba2ea6cbd1a784d97232d20fc60661c364fec00f3d39acb32a7f1051",
    },
    {
        "slot": "self_play_05", "conn": "B", "hand": 1,
        "stage": "preflop", "action": "call",
        "source_record_seq": 16, "source_observation_seq": 11,
        "boundary_record_seq": 18, "boundary_observation_seq": 13,
        "boundary_message": "flop|<1,5><0,2><2,10>",
        "flush_record_seq": 19, "flush_observation_seq": 11,
        "stored_summary_digest": "7c50d9eb926f5124a918391beb366fec940961ff8d54bb5082f896bdf34e3588",
        "finalized_summary_digest": "7c50d9eb926f5124a918391beb366fec940961ff8d54bb5082f896bdf34e3588",
        "provisional_summary_digest": "33e2d95205bccd4fdc0016ad5fb0ab6daf40198eaddbc1a48b2b8f89c8c1c27f",
    },
    {
        "slot": "opponent_02", "conn": "A", "hand": 2,
        "stage": "flop", "action": "call",
        "source_record_seq": 54, "source_observation_seq": 38,
        "boundary_record_seq": 55, "boundary_observation_seq": 39,
        "boundary_message": "turn|<0,3>",
        "flush_record_seq": 57, "flush_observation_seq": 38,
        "stored_summary_digest": "b333968d20af47cb9becfe2dfa551a30a898a3032a6c4e9e9cfe14b5936cd65c",
        "finalized_summary_digest": "b333968d20af47cb9becfe2dfa551a30a898a3032a6c4e9e9cfe14b5936cd65c",
        "provisional_summary_digest": "bed4dc30d07b55aaefe4d15289975ce6bfe339cec60d3394d238fe9217884d8c",
    },
    {
        "slot": "opponent_03", "conn": "B", "hand": 3,
        "stage": "flop", "action": "call",
        "source_record_seq": 91, "source_observation_seq": 64,
        "boundary_record_seq": 93, "boundary_observation_seq": 66,
        "boundary_message": "turn|<1,4>",
        "flush_record_seq": 94, "flush_observation_seq": 64,
        "stored_summary_digest": "eed993b81d3cdf6ef7d379bd2f3efeefb5c7db653b6011b5b2a72d4f7b244e85",
        "finalized_summary_digest": "eed993b81d3cdf6ef7d379bd2f3efeefb5c7db653b6011b5b2a72d4f7b244e85",
        "provisional_summary_digest": "6320756f79e969dad510a281e899b17829692c65654156aeb77656b2b4b0ed14",
    },
)


_V65_THP_PREFIX_FAILURES = (
    {
        "slot": "self_play_03", "hand": 6, "stage": "turn",
        "public_cards_observed": 4, "thp_record_index": 5,
        "thp_cards_payload": "4h5s|4s8d/AcAd3s/4d",
        "thp_sha256": "a9516d06d7e7b093c24468e35376f519cb51df83166a128902049050860e1aab",
        "thp_bytes": 2998,
        "wire_omissions_digest": "cfc0192c3e28981dea562ce29818865634bbe6c36ce183053b1ce0386b0db048",
        "strict_match_digest": "e2dd59a8fcefd6b92e31ee24db5e02eefe71948ee7c9a9e7295a91389f7ba385",
        "prefix_binding_digest": "c74bb00ac329eb9200cbe5580afc0001ac16807ffcebded38a085bdc12111717",
    },
    {
        "slot": "opponent_01", "hand": 1, "stage": "flop",
        "public_cards_observed": 3, "thp_record_index": 0,
        "thp_cards_payload": "AcTh|TdKd/9sAdKh",
        "thp_sha256": "3e8fd8ab81e6e96fedb10c67474851d8bb4bb59bf9ffc372a469ea6eae514d59",
        "thp_bytes": 3496,
        "wire_omissions_digest": "02284d01b7ecce0a6508f4695f4d654abb34587a39affc36eb873f048b812073",
        "strict_match_digest": "5d03200fee62a521a2c45c39a0c21bcc5b94cf37c7ba424fbb9ddcc8b6050f6a",
        "prefix_binding_digest": "3fb1a2349463e49f432faf9e95579d6defe87399fb7cd54970abd7f292acda5f",
    },
)


_V65_DIAGNOSIS_FIELDS = frozenset({
    "schema_version", "kind", "profile_id", "defect_ids",
    "incident_identity", "baseline_wire_probe_sha256",
    "repair_wire_probe_sha256", "baseline_harness_sha256",
    "repair_harness_sha256", "baseline_oracle_document_sha256",
    "repair_oracle_document_sha256", "baseline_oracle_fixture_sha256",
    "repair_oracle_fixture_sha256", "evidence_sha256",
    "evidence_archive_sha256", "evidence_archive_manifest_digest",
    "suite_summary_sha256", "attribution_digest", "round_receipts",
    "live_deferred_failures", "thp_prefix_failures",
    "authority_absence", "strength_evaluation", "disposition",
    "proof_digest",
})


_V65_INCIDENT_IDENTITY_FIELDS = frozenset({
    "baseline_head", "baseline_contract_version", "baseline_contract_hash",
    "repair_contract_version", "workflow_run_id", "checkpoint_revision",
    "candidate_artifact_hash", "job_id", "job_result_digest",
    "rounds_requested", "rounds_completed", "rounds_run", "passed_rounds",
    "failed_rounds",
})


_V65_ROUND_RECEIPT_FIELDS = frozenset({
    "slot", "round_id", "passed", "receipt_sha256",
    "wire_events_sha256", "replay_summary_sha256", "event_count",
    "hands_started", "settlements",
})


_V65_LIVE_FAILURE_FIELDS = frozenset({
    "slot", "round_id", "conn", "hand", "stage", "action",
    "source_record_seq", "source_observation_seq", "boundary_record_seq",
    "boundary_observation_seq", "boundary_message", "flush_record_seq",
    "flush_observation_seq", "stored_summary_digest",
    "finalized_summary_digest", "provisional_summary_digest",
})


_V65_THP_FAILURE_FIELDS = frozenset({
    "slot", "round_id", "hand", "stage", "public_cards_observed",
    "thp_record_index", "thp_cards_payload", "thp_sha256", "thp_bytes",
    "wire_omissions_digest", "strict_match_digest", "prefix_binding_digest",
})


def _expected_called_allin_oracle_observations(*args, **kwargs):
    """Delegate to diagnosis_called_allin."""
    return _bcd._expected_called_allin_oracle_observations(*args, **kwargs)


def _expected_called_allin_incident_identity(*args, **kwargs):
    """Delegate to diagnosis_called_allin."""
    return _bcd._expected_called_allin_incident_identity(*args, **kwargs)


def _called_allin_incident_identity_issues(*args, **kwargs):
    """Delegate to diagnosis_called_allin."""
    return _bcd._called_allin_incident_identity_issues(*args, **kwargs)


def _validate_called_allin_failure_diagnosis_envelope(*args, **kwargs):
    """Delegate to diagnosis_called_allin."""
    return _bcd._validate_called_allin_failure_diagnosis_envelope(*args, **kwargs)


def _expected_v65_incident_identity() -> dict[str, Any]:
    """Delegate to diagnosis_v65."""
    return _dv65._expected_v65_incident_identity()


def _validate_v65_failure_diagnosis_envelope(
    value: Any,
) -> dict[str, Any]:
    """Delegate to diagnosis_v65."""
    return _dv65._validate_v65_failure_diagnosis_envelope(value)


def _validate_contract_failure_diagnosis_envelope(
    value: Any,
) -> dict[str, Any]:
    if isinstance(value, dict) and value.get("kind") == (
        _V65_DIAGNOSIS_KIND
    ):
        return _validate_v65_failure_diagnosis_envelope(value)
    if isinstance(value, dict) and value.get("kind") == (
        _CALLED_ALLIN_DIAGNOSIS_KIND
    ):
        return _validate_called_allin_failure_diagnosis_envelope(value)
    return _validate_causal_failure_diagnosis_envelope(value)


def _validate_causal_failure_diagnosis_envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CAUSAL_FAILURE_DIAGNOSIS_FIELDS:
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_causal_failure_diagnosis_fields_invalid"
        ])
    payload = {key: item for key, item in value.items() if key != "proof_digest"}
    rounds = value.get("rounds")
    digest_fields = (
        "baseline_wire_probe_sha256",
        "repair_wire_probe_sha256",
        "evidence_sha256",
        "evidence_archive_sha256",
        "evidence_archive_manifest_digest",
        "suite_summary_sha256",
        "attribution_digest",
    )
    round_digest_fields = (
        "receipt_sha256",
        "wire_events_sha256",
        "replay_summary_sha256",
        "deferred_observation_bindings_digest",
        "legacy_summary_digest",
        "corrected_summary_digest",
    )
    if (
        value.get("schema_version") != 1
        or value.get("kind") != _CAUSAL_FAILURE_DIAGNOSIS_KIND
        or value.get("defect_id") != _CAUSAL_FAILURE_DEFECT_ID
        or value.get("proof_digest") != canonical_digest(payload)
        or value.get("strength_evaluation") != "not_applicable"
        or value.get("disposition")
        != "abandon_and_reprepare_only_without_evidence_reuse"
        or value.get("original_issue_kinds")
        != sorted(_LEGACY_FALSE_WIRE_ISSUES)
        or any(not _HEX64.fullmatch(str(value.get(field) or "")) for field in digest_fields)
        or value.get("baseline_wire_probe_sha256")
        == value.get("repair_wire_probe_sha256")
        or type(value.get("original_issue_count")) is not int
        or int(value["original_issue_count"]) != 10
        or not isinstance(rounds, list)
        or len(rounds) != 8
        or any(
            not isinstance(item, dict)
            or set(item) != _CAUSAL_FAILURE_ROUND_FIELDS
            for item in rounds
        )
        or [item.get("slot") for item in rounds]
        != [
            "self_play_01",
            "self_play_02",
            "self_play_03",
            "self_play_04",
            "self_play_05",
            "opponent_01",
            "opponent_02",
            "opponent_03",
        ]
        or any(
            not isinstance(item.get("legacy_issue_kinds"), list)
            or not item["legacy_issue_kinds"]
            or any(
                issue not in _LEGACY_FALSE_WIRE_ISSUES
                for issue in item["legacy_issue_kinds"]
            )
            for item in rounds
        )
        or sum(len(item["legacy_issue_kinds"]) for item in rounds) != 10
        or [len(item["legacy_issue_kinds"]) for item in rounds]
        != [1, 2, 1, 1, 1, 1, 2, 1]
        or rounds[0]["legacy_issue_kinds"] != ["illegal_call"]
        or any(
            issue != "unsolicited_client_action"
            for item in rounds[1:]
            for issue in item["legacy_issue_kinds"]
        )
        or any(
            any(not _HEX64.fullmatch(str(item.get(field) or "")) for field in round_digest_fields)
            or not isinstance(item.get("round_id"), str)
            or not item["round_id"].startswith(f"{item['slot']}_")
            or type(item.get("event_count")) is not int
            or type(item.get("stored_events_seen")) is not int
            or not 1 <= item["stored_events_seen"] <= item["event_count"]
            or not isinstance(item.get("max_pending_wait_sec"), (int, float))
            or isinstance(item.get("max_pending_wait_sec"), bool)
            or not math.isfinite(float(item["max_pending_wait_sec"]))
            or not 0.0 <= float(item["max_pending_wait_sec"]) < 60.0
            for item in rounds
        )
        or tuple(item["event_count"] for item in rounds)
        != _LEGACY_INCIDENT_EVENT_COUNTS
        or tuple(item["stored_events_seen"] for item in rounds)
        != _LEGACY_INCIDENT_STORED_COUNTS
        or tuple(item.get("corrected_hands_started") for item in rounds)
        != _LEGACY_INCIDENT_HANDS
        or tuple(item.get("corrected_settlements") for item in rounds)
        != _LEGACY_INCIDENT_SETTLEMENTS
        or any(
            type(item.get("corrected_hands_started")) is not int
            or type(item.get("corrected_settlements")) is not int
            or type(item.get("corrected_pending_count")) is not int
            or not 0 <= item["corrected_hands_started"] < 70
            or not 0 <= item["corrected_settlements"] < 70
            or item["corrected_pending_count"] != 1
            for item in rounds
        )
    ):
        raise BootstrapContractRecoveryError([
            "bootstrap_contract_causal_failure_diagnosis_invalid"
        ])
    return value


def _legacy_causal_failure_diagnosis(
    root: Path,
    directory: Path,
    *,
    request: dict[str, Any],
    state: dict[str, Any],
    status: dict[str, Any],
    candidate_hash: str,
    expected_baseline_head: str,
    expected_repair_head: str,
    require_live_repair_source: bool = True,
) -> dict[str, Any]:
    """Prove the one known 8/8 append-order false-failure profile.

    This produces abandon/reprepare authority only.  The signed failure,
    receipts, archive, and all incomplete rounds remain immutable and retain
    zero certification or strength authority.
    """
    # Lazy import: read the *current* attribute on the main module so that
    # monkeypatch.setattr(recovery, "_git", ...) and "_read_regular_exact" in
    # tests remain effective. Do NOT hoist these to top-level imports.
    from bootstrap_contract_recovery import _git as _git  # noqa: E402
    from bootstrap_contract_recovery import _read_regular_exact as _read_regular_exact  # noqa: E402

    from official_evidence_archive import validate_evidence_archive
    from official_wire_probe import replay_events

    if state.get("attempt") != 1:
        raise ValueError("legacy false-failure job is not attempt one")
    status_job_envelope = status.get("official_job_envelope")
    if not isinstance(status_job_envelope, dict) or not status_job_envelope:
        raise ValueError("status official job envelope is missing")
    identity = request.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    platform = identity.get("platform")
    platform = platform if isinstance(platform, dict) else {}
    baseline_source = _git(
        root,
        "show",
        f"{expected_baseline_head}:web/core/official_wire_probe.py",
        binary=True,
    )
    if not isinstance(baseline_source, bytes):
        raise ValueError("baseline wire probe source is unavailable")
    baseline_wire_sha256 = _sha256_bytes(baseline_source)
    repair_source = _git(
        root,
        "show",
        f"{expected_repair_head}:web/core/official_wire_probe.py",
        binary=True,
    )
    if not isinstance(repair_source, bytes):
        raise ValueError("repair wire probe source is unavailable")
    repair_wire_sha256 = _sha256_bytes(repair_source)
    if (
        platform.get("wire_probe_sha256") != baseline_wire_sha256
        or repair_wire_sha256 == baseline_wire_sha256
    ):
        raise ValueError("wire probe contract change is not proven")
    if require_live_repair_source:
        current_wire_sha256 = _sha256_bytes(_read_regular_exact(
            root / "web" / "core" / "official_wire_probe.py",
            max_bytes=2 * 1024 * 1024,
        ))
        if current_wire_sha256 != repair_wire_sha256:
            raise ValueError("live wire probe is not the reviewed repair")

    suite = directory / "suite_attempt_01"
    _require_regular_directory(suite)
    status_summary = status.get("summary")
    status_summary = status_summary if isinstance(status_summary, dict) else {}
    if Path(str(status_summary.get("suite_dir") or "")) != suite:
        raise ValueError("legacy suite path is not job-owned")
    evidence_path = suite / "official_evidence.json"
    if Path(str(status.get("official_evidence_path") or "")) != evidence_path:
        raise ValueError("legacy evidence path is not canonical")
    summary_raw, suite_report = _regular_json(
        suite / "summary.json",
        max_bytes=4 * 1024 * 1024,
    )
    evidence_raw, evidence = _regular_json(
        evidence_path,
        max_bytes=4 * 1024 * 1024,
    )
    evidence_sha256 = _sha256_bytes(evidence_raw)
    deterministic = status.get("official_deterministic_status_receipt")
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    archive = status.get("official_evidence_archive")
    archive = archive if isinstance(archive, dict) else {}
    archive_validation = validate_evidence_archive(
        archive,
        expected_evidence_sha256=evidence_sha256,
    )
    if (
        deterministic.get("evidence_sha256") != evidence_sha256
        or archive.get("evidence_sha256") != evidence_sha256
        or archive_validation.get("valid") is not True
        or evidence.get("schema_version") != 1
        or evidence.get("purpose") != "official_platform_compliance"
        or evidence.get("strength_evaluation") != "not_applicable"
    ):
        raise ValueError("legacy evidence archive is not exact and retained")

    expected_summary = {
        "self_play_rounds": 5,
        "opponent_rounds": 3,
        "target_hands": 70,
        "rounds_requested": 8,
        "rounds_run": 8,
        "passed_rounds": 0,
        "failed_rounds": 8,
        "resumed_rounds": 0,
        "official_platform": True,
    }
    report_summary = suite_report.get("summary")
    report_summary = report_summary if isinstance(report_summary, dict) else {}
    evidence_summary = evidence.get("summary")
    evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
    if any(
        status_summary.get(key) != expected
        or report_summary.get(key) != expected
        or evidence_summary.get(key) != expected
        for key, expected in expected_summary.items()
    ):
        raise ValueError("legacy suite is not exact failed 5+3x70")
    if (
        Path(str(report_summary.get("suite_dir") or "")) != suite
        or Path(str(evidence_summary.get("suite_dir") or "")) != suite
    ):
        raise ValueError("legacy suite summary path changed")

    attribution = status_summary.get("attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    attribution_rounds = attribution.get("rounds")
    if (
        attribution.get("schema_version") != 1
        or attribution.get("policy_id") != "official-attribution-v1"
        or attribution.get("candidate_verdict") != "fail"
        or attribution.get("candidate_blocking") is not True
        or attribution.get("inconclusive") is not False
        or attribution.get("countable_rounds") != 0
        or not isinstance(attribution_rounds, list)
        or len(attribution_rounds) != 8
        or report_summary.get("attribution") != attribution
        or evidence_summary.get("attribution") != attribution
        or status_summary.get("formal_execution")
        != report_summary.get("formal_execution")
        or report_summary.get("formal_execution")
        != evidence_summary.get("formal_execution")
        or not isinstance(report_summary.get("formal_execution"), dict)
        or report_summary["formal_execution"].get("ok") is not True
        or report_summary["formal_execution"].get("issues") != []
        or evidence_summary.get("passed") is not False
        or evidence_summary.get("raw_passed") is not False
        or evidence_summary.get("wire_evidence_required_rounds") != 8
        or evidence_summary.get("wire_evidence_complete_rounds") != 8
    ):
        raise ValueError("legacy failure attribution is invalid")

    expected_slots = [
        *(f"self_play_{index:02d}" for index in range(1, 6)),
        *(f"opponent_{index:02d}" for index in range(1, 4)),
    ]
    report_rounds = suite_report.get("rounds")
    evidence_rounds = evidence.get("rounds")
    if (
        not isinstance(report_rounds, list)
        or len(report_rounds) != 8
        or not isinstance(evidence_rounds, list)
        or len(evidence_rounds) != 8
    ):
        raise ValueError("legacy suite round set is incomplete")

    proof_rounds: list[dict[str, Any]] = []
    all_legacy_issue_kinds: list[str] = []
    for offset, slot in enumerate(expected_slots):
        kind = "self_play" if slot.startswith("self_play") else "opponent"
        index = int(slot.rsplit("_", 1)[1])
        receipt = report_rounds[offset]
        evidence_round = evidence_rounds[offset]
        attribution_round = attribution_rounds[offset]
        if not all(
            isinstance(item, dict)
            for item in (receipt, evidence_round, attribution_round)
        ):
            raise ValueError("legacy round evidence shape is invalid")
        round_id = receipt.get("round_id")
        if (
            receipt.get("round_kind") != kind
            or receipt.get("round_index") != index
            or receipt.get("target_hands") != 70
            or receipt.get("passed") is not False
            or not isinstance(round_id, str)
            or not round_id.startswith(f"{slot}_")
            or evidence_round.get("round_kind") != kind
            or evidence_round.get("round_index") != index
            or evidence_round.get("round_id") != round_id
            or evidence_round.get("passed") is not False
            or evidence_round.get("strength_evaluation") not in {None, "not_applicable"}
        ):
            raise ValueError("legacy round identity is invalid")
        round_envelope = receipt.get("job_envelope")
        _require_exact_round_job_envelope(
            round_envelope,
            status_job_envelope,
            job_id=directory.name,
            candidate_hash=candidate_hash,
        )
        wire_probe = receipt.get("wire_probe")
        wire_probe = wire_probe if isinstance(wire_probe, dict) else {}
        if wire_probe.get("enabled") is not True or wire_probe.get("issues") != []:
            raise ValueError("legacy round wire probe failed independently")

        round_attribution_topology = attribution_round.get("topology")
        round_attribution_topology = (
            round_attribution_topology
            if isinstance(round_attribution_topology, dict)
            else {}
        )
        findings = attribution_round.get("findings")
        if (
            attribution_round.get("schema_version") != 1
            or attribution_round.get("policy_id") != "official-attribution-v1"
            or round_attribution_topology.get("round_kind") != kind
            or not isinstance(findings, list)
        ):
            raise ValueError("legacy round attribution is invalid")
        wire_findings: list[dict[str, Any]] = []
        downstream_codes: list[str] = []
        for finding in findings:
            if not isinstance(finding, dict) or finding.get("round_id") != round_id:
                raise ValueError("legacy attribution finding is unbound")
            code = finding.get("code")
            if code in _LEGACY_FALSE_WIRE_ISSUES:
                evidence_item = finding.get("evidence")
                evidence_item = evidence_item if isinstance(evidence_item, dict) else {}
                subject = finding.get("subject_domain")
                if (
                    finding.get("category") != "protocol"
                    or finding.get("certainty") != "deterministic"
                    or subject not in {"candidate", "opponent"}
                    or finding.get("candidate_impact")
                    != ("block" if subject == "candidate" else "retry")
                    or evidence_item.get("kind") != code
                    or evidence_item.get("conn") not in {"A", "B"}
                ):
                    raise ValueError("legacy wire finding attribution is invalid")
                wire_findings.append(finding)
            elif code in _LEGACY_DOWNSTREAM_FINDINGS:
                if (
                    finding.get("category") != "harness"
                    or finding.get("subject_domain") != "harness"
                    or finding.get("candidate_impact") != "retry"
                    or (finding.get("evidence") or {}).get("issue") != code
                ):
                    raise ValueError("legacy downstream finding is invalid")
                downstream_codes.append(str(code))
            else:
                raise ValueError("legacy failure contains another finding")
        if (
            not wire_findings
            or sorted(downstream_codes) != sorted(_LEGACY_DOWNSTREAM_FINDINGS)
        ):
            raise ValueError("legacy round findings are incomplete")

        artifacts = evidence_round.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        receipt_item = artifacts.get("receipt")
        archive_path = str((receipt_item or {}).get("archive_path") or "")
        pure_receipt = PurePosixPath(archive_path)
        if (
            len(pure_receipt.parts) != 4
            or pure_receipt.parts[0] != slot
            or pure_receipt.parts[1] != "executions"
            or re.fullmatch(r"run_[0-9]+_[0-9]+", pure_receipt.parts[2]) is None
            or pure_receipt.parts[3] != "receipt.json"
        ):
            raise ValueError("legacy round execution path is invalid")
        execution_prefix = "/".join(pure_receipt.parts[:-1])
        receipt_raw = _strict_artifact_bytes(
            suite,
            receipt_item,
            expected_archive_path=f"{execution_prefix}/receipt.json",
            max_bytes=1024 * 1024,
        )
        observed_receipt = json.loads(receipt_raw.decode("utf-8"))
        if observed_receipt != receipt:
            raise ValueError("legacy summary is not the exact round receipt")
        slot_dir = suite / slot
        executions = slot_dir / "executions"
        execution_dir = executions / pure_receipt.parts[2]
        for owned_directory in (slot_dir, executions, execution_dir):
            _require_regular_directory(owned_directory)
        if (
            sorted(item.name for item in slot_dir.iterdir())
            != ["executions", "receipt.json"]
            or sorted(item.name for item in executions.iterdir())
            != [pure_receipt.parts[2]]
            or _read_regular_exact(slot_dir / "receipt.json", max_bytes=1024 * 1024)
            != receipt_raw
        ):
            raise ValueError("legacy round has a resumed or duplicate execution")
        wire_raw = _strict_artifact_bytes(
            suite,
            artifacts.get("wire_events"),
            expected_archive_path=f"{execution_prefix}/wire_events.jsonl",
            max_bytes=1024 * 1024,
        )
        replay_raw = _strict_artifact_bytes(
            suite,
            artifacts.get("replay_summary"),
            expected_archive_path=f"{execution_prefix}/replay_summary.json",
            max_bytes=1024 * 1024,
        )
        stored_replay = json.loads(replay_raw.decode("utf-8"))
        if (
            not isinstance(stored_replay, dict)
            or receipt.get("wire_replay_summary") != stored_replay
            or evidence_round.get("wire_replay_summary") != stored_replay
        ):
            raise ValueError("legacy replay summary is not cross-bound")
        events = [
            json.loads(line)
            for line in wire_raw.decode("utf-8").splitlines()
            if line.strip()
        ]
        legacy_summary_digest = _legacy_replay_matches_stored(
            events,
            stored_replay,
        )
        legacy_issue_kinds = [
            str(item.get("kind") or "")
            for item in (stored_replay.get("issues") or [])
            if isinstance(item, dict)
        ]
        if (
            not legacy_issue_kinds
            or any(kind_name not in _LEGACY_FALSE_WIRE_ISSUES for kind_name in legacy_issue_kinds)
            or legacy_issue_kinds
            != [str(item.get("code")) for item in wire_findings]
            or stored_replay.get("warnings") != []
        ):
            raise ValueError("legacy replay has a different failure")
        evidence_attribution = evidence_round.get("attribution")
        evidence_attribution = (
            evidence_attribution
            if isinstance(evidence_attribution, dict)
            else {}
        )
        evidence_findings = evidence_attribution.get("findings")
        evidence_findings = evidence_findings if isinstance(evidence_findings, list) else []
        replay_findings = [
            finding
            for finding in evidence_findings
            if isinstance(finding, dict) and finding.get("code") == "wire_replay"
        ]
        if len(replay_findings) != 1:
            raise ValueError("legacy evidence replay finding is not unique")
        replay_finding = replay_findings[0]
        replay_issue = str((replay_finding.get("evidence") or {}).get("issue") or "")
        if (
            replay_finding.get("round_id") != round_id
            or replay_finding.get("category") != "protocol"
            or replay_finding.get("subject_domain") != "harness"
            or replay_finding.get("subject_instance_id") != "official_harness"
            or replay_finding.get("candidate_impact") != "retry"
            or replay_finding.get("certainty") != "deterministic"
            or replay_finding.get("connection") != ""
            or replay_issue
            not in {
                f"wire_replay: {kind_name}"
                for kind_name in set(legacy_issue_kinds)
            }
        ):
            raise ValueError("legacy evidence replay finding is not derived")
        normalized_evidence_attribution = dict(evidence_attribution)
        normalized_evidence_attribution["findings"] = [
            finding for finding in evidence_findings
            if finding is not replay_finding
        ]
        normalized_evidence_attribution["retry_finding_ids"] = [
            finding_id
            for finding_id in (evidence_attribution.get("retry_finding_ids") or [])
            if finding_id != replay_finding.get("finding_id")
        ]
        if normalized_evidence_attribution != attribution_round:
            raise ValueError("legacy evidence attribution is not cross-bound")
        round_issues = receipt.get("issues")
        evidence_issues = evidence_round.get("issues")
        if not isinstance(round_issues, list) or any(
            not isinstance(item, str) for item in round_issues
        ):
            raise ValueError("legacy round issue list is invalid")
        expected_wire_issue_prefixes = [f"wire_{kind_name}:" for kind_name in legacy_issue_kinds]
        observed_wire_issues = [
            item for item in round_issues
            if any(item.startswith(prefix) for prefix in expected_wire_issue_prefixes)
        ]
        if (
            len(observed_wire_issues) != len(legacy_issue_kinds)
            or sorted(
                item for item in round_issues if item not in observed_wire_issues
            ) != sorted(_LEGACY_DOWNSTREAM_FINDINGS)
            or not isinstance(evidence_issues, list)
            or sorted(evidence_issues)
            != sorted([
                *round_issues,
                *(f"wire_replay: {kind_name}" for kind_name in sorted(set(legacy_issue_kinds))),
            ])
        ):
            raise ValueError("legacy round issues contain another failure")

        causal_events, bindings = _legacy_wire_causalize(events)
        frozen_now = max(float(event["t"]) for event in events)
        corrected = replay_events(
            causal_events,
            now=frozen_now,
            finalized=False,
        )
        corrected_legacy_projection = _legacy_owned_replay_projection(
            corrected
        )
        pending_actions = corrected.get("pending_expected_actions")
        pending_actions = pending_actions if isinstance(pending_actions, list) else []
        pending_waits = [
            float(item.get("waited_sec", 0.0) or 0.0)
            for item in pending_actions
            if isinstance(item, dict)
        ]
        max_pending_wait = max(pending_waits, default=0.0)
        corrected_hands = corrected.get("hands_started_min")
        corrected_settlements = corrected.get("settlements_min")
        corrected_pending_count = len(pending_actions)
        if (
            corrected.get("issues") != []
            or corrected.get("warnings") != []
            or corrected.get("events_seen") != len(events)
            or not 0.0 <= max_pending_wait < 60.0
            or corrected_hands != _LEGACY_INCIDENT_HANDS[offset]
            or corrected_settlements
            != _LEGACY_INCIDENT_SETTLEMENTS[offset]
            or type(corrected_hands) is not int
            or type(corrected_settlements) is not int
            or not 0 <= corrected_hands < 70
            or not 0 <= corrected_settlements < 70
            or corrected_pending_count != 1
        ):
            raise ValueError("causal replay does not clear only the old defect")

        all_legacy_issue_kinds.extend(legacy_issue_kinds)
        proof_rounds.append({
            "slot": slot,
            "round_id": round_id,
            "receipt_sha256": _sha256_bytes(receipt_raw),
            "wire_events_sha256": _sha256_bytes(wire_raw),
            "replay_summary_sha256": _sha256_bytes(replay_raw),
            "event_count": len(events),
            "stored_events_seen": stored_replay["events_seen"],
            "legacy_issue_kinds": legacy_issue_kinds,
            "deferred_observation_bindings_digest": canonical_digest(bindings),
            "legacy_summary_digest": legacy_summary_digest,
            "corrected_summary_digest": canonical_digest(
                corrected_legacy_projection
            ),
            "max_pending_wait_sec": round(max_pending_wait, 3),
            "corrected_hands_started": corrected_hands,
            "corrected_settlements": corrected_settlements,
            "corrected_pending_count": corrected_pending_count,
        })

    if (
        len(all_legacy_issue_kinds) != 10
        or set(all_legacy_issue_kinds) != _LEGACY_FALSE_WIRE_ISSUES
    ):
        raise ValueError("legacy failure is not the exact ten-finding defect")
    payload = {
        "schema_version": 1,
        "kind": _CAUSAL_FAILURE_DIAGNOSIS_KIND,
        "defect_id": _CAUSAL_FAILURE_DEFECT_ID,
        "baseline_wire_probe_sha256": baseline_wire_sha256,
        "repair_wire_probe_sha256": repair_wire_sha256,
        "evidence_sha256": evidence_sha256,
        "evidence_archive_sha256": archive["archive_sha256"],
        "evidence_archive_manifest_digest": archive["manifest_digest"],
        "suite_summary_sha256": _sha256_bytes(summary_raw),
        "attribution_digest": canonical_digest(attribution),
        "original_issue_kinds": sorted(set(all_legacy_issue_kinds)),
        "original_issue_count": len(all_legacy_issue_kinds),
        "rounds": proof_rounds,
        "strength_evaluation": "not_applicable",
        "disposition": "abandon_and_reprepare_only_without_evidence_reuse",
    }
    return _validate_causal_failure_diagnosis_envelope({
        **payload,
        "proof_digest": canonical_digest(payload),
    })


def _called_allin_oracle_identity(*args, **kwargs):
    """Delegate to diagnosis_called_allin."""
    return _bcd._called_allin_oracle_identity(*args, **kwargs)


def _called_allin_authority_absence(*args, **kwargs):
    """Delegate to diagnosis_called_allin."""
    return _bcd._called_allin_authority_absence(*args, **kwargs)


def _called_allin_runout_failure_diagnosis(*args, **kwargs):
    """Delegate to diagnosis_called_allin."""
    return _bcd._called_allin_runout_failure_diagnosis(*args, **kwargs)


def _v65_contract_failure_diagnosis(
    root: Path,
    directory: Path,
    *,
    request: dict[str, Any],
    state: dict[str, Any],
    status: dict[str, Any],
    candidate_hash: str,
    workflow_run_id: str,
    checkpoint_revision: int,
    job_result_digest: str,
    expected_evaluation_contract_version: int,
    expected_evaluation_contract_hash: str,
    expected_repair_contract_version: int,
    expected_baseline_head: str,
    expected_repair_head: str,
    control_consumption: dict[str, Any],
    require_live_repair_source: bool = True,
) -> dict[str, Any]:
    """Delegate to diagnosis_v65."""
    return _dv65._v65_contract_failure_diagnosis(
        root,
        directory,
        request=request,
        state=state,
        status=status,
        candidate_hash=candidate_hash,
        workflow_run_id=workflow_run_id,
        checkpoint_revision=checkpoint_revision,
        job_result_digest=job_result_digest,
        expected_evaluation_contract_version=expected_evaluation_contract_version,
        expected_evaluation_contract_hash=expected_evaluation_contract_hash,
        expected_repair_contract_version=expected_repair_contract_version,
        expected_baseline_head=expected_baseline_head,
        expected_repair_head=expected_repair_head,
        control_consumption=control_consumption,
        require_live_repair_source=require_live_repair_source,
    )
