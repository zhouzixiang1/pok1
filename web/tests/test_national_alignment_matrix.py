"""Regression coverage for the executable national cross-layer matrix."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import national_alignment_matrix as matrix


ROOT = Path(__file__).resolve().parents[2]


def _current_row() -> matrix.MatrixRow:
    return next(
        row
        for row in matrix.CURRENT_ALIGNMENT_ROWS
        if row.status == matrix.CURRENT_STATUS
    )


def _raw_name_handshake_row() -> matrix.MatrixRow:
    return next(
        row
        for row in matrix.CURRENT_ALIGNMENT_ROWS
        if row.rule_id == "raw_tcp_name_handshake"
    )


def _quality_runtime_identity_row() -> matrix.MatrixRow:
    return next(
        row
        for row in matrix.CURRENT_ALIGNMENT_ROWS
        if row.rule_id == matrix._QUALITY_RUNTIME_IDENTITY_RULE_ID
    )


def _frontend_status_row() -> matrix.MatrixRow:
    return next(
        row
        for row in matrix.CURRENT_ALIGNMENT_ROWS
        if row.rule_id == "frontend_authoritative_status"
    )


def test_checked_in_matrix_has_all_required_current_cross_layer_coverage():
    assert matrix.validate_alignment_matrix() == []
    observed = {
        item
        for row in matrix.CURRENT_ALIGNMENT_ROWS
        if row.status == matrix.CURRENT_STATUS
        for item in row.coverage
    }
    assert matrix.REQUIRED_COVERAGE <= observed
    assert any(
        row.status == matrix.SUPERSEDED_STATUS
        and row.evidence_state == matrix.HISTORICAL
        for row in matrix.CURRENT_ALIGNMENT_ROWS
    )
    for row in matrix.CURRENT_ALIGNMENT_ROWS:
        assert row.authority, row.rule_id
        assert row.production_owners, row.rule_id
        assert row.dynamic_gates, row.rule_id
        assert row.prompts, row.rule_id
        assert row.positive_tests and row.negative_tests, row.rule_id
        assert row.fail_closed, row.rule_id
        if row.status == matrix.CURRENT_STATUS:
            assert matrix.REQUIRED_PROMPT_ROLES <= {
                binding.role for binding in row.prompts
            }
            assert row.prompt_statement.strip(), row.rule_id
            assert row.prompt_required_terms, row.rule_id


def test_raw_name_handshake_row_binds_real_slow_import_wire_and_precommit_fail_closed():
    row = _raw_name_handshake_row()

    assert row.status == matrix.CURRENT_STATUS
    assert row.evidence_state == matrix.SOURCE_CONTRACT
    assert row.coverage == ("raw_tcp_name_handshake",)
    assert matrix.REQUIRED_PROMPT_ROLES <= {binding.role for binding in row.prompts}
    assert "all five rendered roles" in row.prompt_statement.lower()
    assert {
        "web/tests/test_national_raw_tcp_handshake.py::"
        "test_generated_native_bot_replies_to_raw_name_before_slow_worker_import",
        "web/tests/test_national_native_strict_artifacts.py::"
        "test_real_system_native_pair_records_one_valid_name_worker_handshake",
    } <= set(row.positive_tests)
    assert {
        "web/tests/test_national_native_strict_artifacts.py::"
        "test_system_native_name_handshake_evidence_is_required_but_legacy_fixture_is_not",
        "web/tests/test_national_runtime_telemetry.py::"
        "test_name_handshake_telemetry_preserves_duplicate_failed_and_malformed_evidence",
        "web/tests/test_national_native_strict_artifacts.py::"
        "test_native_precommit_rejects_name_handshake_compliance_failure",
    } <= set(row.negative_tests)
    wording = " ".join((
        row.prompt_statement,
        row.producer_consumer,
        row.fail_closed,
    )).lower()
    for term in matrix._RAW_NAME_HANDSHAKE_REQUIRED_TERMS:
        assert term in wording


def test_raw_name_handshake_row_fails_closed_when_slow_import_or_precommit_anchor_drifts():
    row = _raw_name_handshake_row()
    broken = replace(
        row,
        positive_tests=tuple(
            item
            for item in row.positive_tests
            if "slow_worker_import" not in item
        ),
        producer_consumer=row.producer_consumer.replace(
            "first decision clock", "decision timer"
        ),
        prompt_statement=row.prompt_statement.replace(
            "first decision clock", "decision timer"
        ),
    )

    errors = matrix.validate_alignment_matrix((
        *(
            item
            for item in matrix.CURRENT_ALIGNMENT_ROWS
            if item.rule_id != row.rule_id
        ),
        broken,
    ))

    assert any(
        error.startswith("matrix_raw_name_handshake_positive_missing:")
        for error in errors
    )
    assert "matrix_raw_name_handshake_semantics_missing:first decision clock" in errors


def test_quality_row_binds_two_file_identity_and_precompute_drift_regressions():
    row = _quality_runtime_identity_row()

    assert row.status == matrix.CURRENT_STATUS
    assert row.evidence_state == matrix.SOURCE_CONTRACT
    assert matrix.REQUIRED_PROMPT_ROLES <= {binding.role for binding in row.prompts}
    assert set(matrix._QUALITY_RUNTIME_IDENTITY_REQUIRED_OWNER_SYMBOLS) <= {
        owner.display() for owner in row.production_owners
    }
    assert set(matrix._QUALITY_RUNTIME_IDENTITY_REQUIRED_POSITIVE_TESTS) <= set(
        row.positive_tests
    )
    assert set(matrix._QUALITY_RUNTIME_IDENTITY_REQUIRED_NEGATIVE_TESTS) <= set(
        row.negative_tests
    )
    wording = " ".join((
        row.prompt_statement,
        row.producer_consumer,
        row.fail_closed,
    )).casefold()
    for term in matrix._QUALITY_RUNTIME_IDENTITY_REQUIRED_TERMS:
        assert term.casefold() in wording


def test_quality_row_fails_closed_when_composite_identity_anchor_drifts():
    row = _quality_runtime_identity_row()
    broken = replace(
        row,
        production_owners=tuple(
            owner
            for owner in row.production_owners
            if owner.symbol != "current_system_native_runtime_identity"
        ),
        negative_tests=tuple(
            item
            for item in row.negative_tests
            if "precompute_only_drift" not in item
        ),
        prompt_statement=row.prompt_statement.replace(
            "precompute-only drift", "system drift"
        ),
        fail_closed=row.fail_closed.replace(
            "precompute-only drift", "system drift"
        ),
    )

    errors = matrix.validate_alignment_matrix((
        *(
            item
            for item in matrix.CURRENT_ALIGNMENT_ROWS
            if item.rule_id != row.rule_id
        ),
        broken,
    ))

    assert (
        "matrix_quality_runtime_identity_owner_missing:"
        "web/core/national_runtime_authority.py::"
        "current_system_native_runtime_identity"
        in errors
    )
    assert any(
        error.startswith("matrix_quality_runtime_identity_negative_missing:")
        for error in errors
    )
    assert (
        "matrix_quality_runtime_identity_semantics_missing:precompute-only drift"
        in errors
    )


def test_frontend_status_row_binds_live_task_owner_across_all_consumers():
    row = _frontend_status_row()

    assert row.status == matrix.CURRENT_STATUS
    assert row.evidence_state == matrix.SOURCE_CONTRACT
    owners = {owner.display() for owner in row.production_owners}
    assert {
        "web/core/web_ui.py::set_status",
        "web/server/state.py::task_snapshot",
        "web/server/state.py::add_task_snapshot_listener",
        "web/server/state.py::_advance_task_lifecycle_locked",
        "web/server/app.py::_publish_task_owner",
        "web/server/routes/evolution.py::_current_transient_status",
        "web/server/routes/evolution.py::_task_owner_event_is_current",
        "web/frontend/src/api/evolution.ts::EvolutionState",
        "web/frontend/src/lib/evolutionStreamController.ts::"
        "evolutionStatusMatchesActiveGeneration",
        "web/frontend/src/lib/evolutionStreamController.ts::"
        "shouldAcceptEvolutionStatus",
        "web/frontend/src/lib/evolutionStreamController.ts::"
        "isFreshEvolutionStatusEvent",
        "web/frontend/src/lib/evolutionStreamController.ts::"
        "transientStatusTaskMatches",
        "web/frontend/src/pages/EvolutionMonitor.tsx::acceptTransientStatus",
    } <= owners
    wording = " ".join((
        row.prompt_statement,
        row.producer_consumer,
        row.fail_closed,
    )).casefold()
    assert "task owner" in wording
    assert "replaced-owner" in wording
    assert "authority-gated" in wording
    assert "lifecycle revision" in wording
    assert {
        "web/tests/test_routes_control.py::TestEvolutionTaskOwnership::"
        "test_task_owner_listener_observes_replacement_without_polling",
        "web/tests/test_routes_evolution.py::TestEvolutionStream::"
        "test_task_owner_broadcast_is_minimal_typed_invalidation",
    } <= set(row.positive_tests)
    assert {
        "web/tests/test_routes_evolution.py::TestEvolutionState::"
        "test_state_rejects_status_from_replaced_task_owner",
        "web/tests/test_routes_evolution.py::TestEvolutionStream::"
        "test_status_replay_filter_rejects_stale_inactive_or_wrong_revision",
        "web/tests/test_routes_evolution.py::TestEvolutionStream::"
        "test_task_owner_event_replay_rejects_replaced_owner",
    } <= set(row.negative_tests)


def test_matrix_fails_closed_when_a_required_field_is_removed():
    row = _current_row()
    broken = replace(row, positive_tests=())

    errors = matrix.validate_alignment_matrix((broken,))

    assert f"matrix_positive_tests_missing:{row.rule_id}" in errors
    assert any(error.startswith("matrix_required_coverage_missing:") for error in errors)


def test_current_row_fails_closed_when_any_required_role_prompt_is_removed():
    row = _current_row()
    broken = replace(
        row,
        prompts=tuple(
            binding for binding in row.prompts if binding.role != "Critic"
        ),
    )

    errors = matrix.validate_alignment_matrix((broken,))

    assert (
        f"matrix_current_prompt_roles_missing:{row.rule_id}:Critic"
        in errors
    )


def test_current_row_fails_closed_when_prompt_statement_or_rendered_contract_drifts():
    row = _current_row()
    missing_statement = replace(row, prompt_statement="", prompt_required_terms=())
    invented_term = "never-rendered-matrix-term"
    term_drift = replace(row, prompt_required_terms=(invented_term,))
    first_binding = row.prompts[0]
    overlay_drift = replace(
        row,
        prompts=(
            replace(
                first_binding,
                renderer=matrix.SourceRef(
                    "web/core/national_alignment_matrix.py",
                    "_safe_repo_path",
                ),
            ),
            *row.prompts[1:],
        ),
    )

    missing_errors = matrix.validate_alignment_matrix((missing_statement,))
    term_errors = matrix.validate_alignment_matrix((term_drift,))
    overlay_errors = matrix.validate_alignment_matrix((overlay_drift,))

    assert f"matrix_prompt_statement_missing:{row.rule_id}" in missing_errors
    assert f"matrix_prompt_required_terms_missing:{row.rule_id}" in missing_errors
    assert (
        f"matrix_prompt_statement_term_missing:{row.rule_id}:{invented_term}"
        in term_errors
    )
    assert any(
        error.startswith(
            f"matrix_prompt_rendered_term_missing:{row.rule_id}:"
        )
        and error.endswith(f":{invented_term}")
        for error in term_errors
    )
    assert (
        f"matrix_prompt_renderer_overlay_missing:{row.rule_id}:{first_binding.role}"
        in overlay_errors
    )


def test_matrix_fails_closed_when_an_owner_path_or_symbol_drifts():
    row = _current_row()
    broken_path = replace(
        row,
        production_owners=(matrix.SourceRef("web/core/not_a_real_owner.py", "gone"),),
    )
    broken_symbol = replace(
        row,
        dynamic_gates=(matrix.SourceRef("web/core/national_native.py", "not_a_gate"),),
    )

    path_errors = matrix.validate_alignment_matrix((broken_path,))
    symbol_errors = matrix.validate_alignment_matrix((broken_symbol,))

    assert any(error.endswith(":missing_path") for error in path_errors)
    assert any(error.endswith(":missing_symbol") for error in symbol_errors)


def test_matrix_accepts_checked_frontend_and_shell_dynamic_gate_symbols():
    js_errors = matrix._validate_ref(
        matrix.SourceRef(
            "web/frontend/scripts/static-build-receipt.mjs",
            "verifyReceipt",
        ),
        row_id="frontend_authoritative_status",
        field="dynamic_gate",
        require_symbol=True,
    )
    shell_errors = matrix._validate_ref(
        matrix.SourceRef("pokctl.sh", "cmd_verify_frontend_static"),
        row_id="frontend_authoritative_status",
        field="dynamic_gate",
        require_symbol=True,
    )

    assert js_errors == []
    assert shell_errors == []


def test_current_row_rejects_archive_reference_but_superseded_history_is_explicit():
    row = _current_row()
    current_archive = replace(
        row,
        authority=(matrix.SourceRef("archive/README.md", "legacy-untrusted"),),
    )

    errors = matrix.validate_alignment_matrix((current_archive,))

    assert (
        "matrix_current_archive_reference:"
        f"{row.rule_id}:archive/README.md::legacy-untrusted"
    ) in errors
    historical = next(
        item
        for item in matrix.CURRENT_ALIGNMENT_ROWS
        if item.status == matrix.SUPERSEDED_STATUS
    )
    assert historical.historical_reason
    assert not any(
        error.startswith("matrix_current_archive_reference:")
        for error in matrix.validate_alignment_matrix((historical,))
    )


def test_current_row_rejects_archive_path_hidden_in_producer_consumer_text():
    row = _current_row()
    broken = replace(
        row,
        producer_consumer="native producer → archive/legacy-consumer",
    )

    errors = matrix.validate_alignment_matrix((broken,))

    assert (
        f"matrix_current_archive_text_reference:{row.rule_id}:producer_consumer"
        in errors
    )


def test_human_matrix_current_registry_is_exactly_generated_from_contract():
    document = (ROOT / "docs" / "national-tcp-evolution-alignment-matrix.md").read_text(
        encoding="utf-8"
    )
    begin = "<!-- executable-national-alignment-matrix:begin -->"
    end = "<!-- executable-national-alignment-matrix:end -->"
    assert document.count(begin) == 1
    assert document.count(end) == 1
    start = document.index(begin)
    finish = document.index(end, start) + len(end)
    rendered = matrix.render_current_matrix_markdown().rstrip("\n")
    assert document[start:finish] == rendered


def test_contract38_hand_authored_overlay_tracks_two_file_runtime_identity():
    document = (ROOT / "docs" / "national-tcp-evolution-alignment-matrix.md").read_text(
        encoding="utf-8"
    )
    start = document.index("## Contract-38 superseding overlay")
    finish = document.index(
        "<!-- executable-national-alignment-matrix:begin -->",
        start,
    )
    overlay = document[start:finish]

    assert "exact `NATIVE_BOT_TEMPLATE` identity plus digest" not in overlay
    for required in (
        "`national_bot.py` + `precompute.py`",
        "SHA-256/size",
        "`combined_digest`",
        "precompute-only-drifted",
        "formal admission",
    ):
        assert required in overlay
