"""Executable cross-layer contract for ``national_tcp_policy_v1``.

The long-form alignment document is useful to operators, but it must not be the
only place where a rule's ownership and proof are recorded.  This module is the
machine-readable current view.  It deliberately validates *references*, not
runtime success: rows such as v143/the first successor and the ten-generation observation are
current obligations whose ``runtime_pending`` state must not be rendered as a
completed production claim.

This module is stdlib-only and has no runtime side effects.  It is intentionally
not imported by the evolution daemon; the accompanying regression test is the
quality gate that makes drift fail before merge.

The large ``CURRENT_ALIGNMENT_ROWS`` literal and its supporting dataclasses,
constants, and prompt registry live in the companion
:mod:`national_alignment_matrix_data` module; this file keeps the validation
and rendering logic and re-imports the names it needs.
"""

from __future__ import annotations

import ast
import gzip
from pathlib import Path
import re
from typing import Iterable, Sequence

from national_alignment_matrix_data import (
    CURRENT_ALIGNMENT_ROWS,
    CURRENT_STATUS,
    HISTORICAL,
    MATRIX_SCHEMA_VERSION,
    MatrixRow,
    PromptBinding,
    REQUIRED_COVERAGE,
    REQUIRED_PROMPT_ROLES,
    RUNTIME_PENDING,
    SOURCE_CONTRACT,
    SUPERSEDED_STATUS,
    SourceRef,
    _QUALITY_RUNTIME_IDENTITY_REQUIRED_NEGATIVE_TESTS,
    _QUALITY_RUNTIME_IDENTITY_REQUIRED_OWNER_SYMBOLS,
    _QUALITY_RUNTIME_IDENTITY_REQUIRED_POSITIVE_TESTS,
    _QUALITY_RUNTIME_IDENTITY_REQUIRED_TERMS,
    _QUALITY_RUNTIME_IDENTITY_RULE_ID,
    _RAW_NAME_HANDSHAKE_REQUIRED_NEGATIVE_TESTS,
    _RAW_NAME_HANDSHAKE_REQUIRED_POSITIVE_TESTS,
    _RAW_NAME_HANDSHAKE_REQUIRED_TERMS,
    _RAW_NAME_HANDSHAKE_RULE_ID,
    _RULE_ID_RE,
    _VALID_EVIDENCE_STATES,
    _VALID_STATUSES,
)


ROOT = Path(__file__).resolve().parents[2]


def _is_archive_path(path: str) -> bool:
    return "archive" in Path(path).parts


def _companion_template_text(source_path: Path) -> str | None:
    """Return the text of a checked-in companion template blob, if any.

    Some system-owned template modules store their large ``NATIVE_*_TEMPLATE``
    string literal in a sibling ``.bin`` file (gzip-compressed or raw UTF-8)
    and decompress it at import time, instead of inlining the literal in the
    ``.py`` source.  A symbol that lives in such a template value is therefore
    absent from the loader's ``.py`` text even though it remains part of the
    system-owned artifact.  This helper exposes that companion text so symbol
    validation can still bind to it without importing the loader (which would
    add a runtime side effect to this otherwise pure, stdlib-only module).
    Returns ``None`` when there is no companion blob.
    """
    blob = source_path.with_suffix(".bin")
    if not blob.is_file():
        return None
    raw = blob.read_bytes()
    try:
        decompressed = gzip.decompress(raw)
    except OSError:
        # Not gzipped; treat the raw bytes as the template text.
        decompressed = raw
    try:
        return decompressed.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _safe_repo_path(path: str) -> Path | None:
    candidate = Path(path)
    if candidate.is_absolute() or ".." in candidate.parts or not path:
        return None
    resolved = ROOT / candidate
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        return None
    return resolved


def _validate_ref(
    ref: SourceRef,
    *,
    row_id: str,
    field: str,
    require_symbol: bool = False,
) -> list[str]:
    errors: list[str] = []
    path = _safe_repo_path(ref.path)
    prefix = f"matrix_{field}:{row_id}:{ref.display()}"
    if path is None:
        return [f"{prefix}:unsafe_path"]
    if not path.is_file():
        return [f"{prefix}:missing_path"]
    if require_symbol and not ref.symbol:
        return [f"{prefix}:missing_symbol"]
    if ref.symbol:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return [f"{prefix}:unreadable:{type(exc).__name__}"]
        # A symbol may legitimately live in a system-owned template value that
        # the loader module decompresses from a checked-in ``.bin`` companion
        # rather than inlining as a literal in the ``.py`` source.  Treat that
        # companion text as part of the module's source surface so the contract
        # keeps binding to the real artifact after such a storage refactor.
        companion = _companion_template_text(path) if ref.symbol not in source else None
        if ref.symbol not in source and (
            companion is None or ref.symbol not in companion
        ):
            errors.append(f"{prefix}:missing_symbol")
        elif require_symbol:
            if path.suffix == ".py":
                try:
                    tree = ast.parse(source, filename=str(path))
                except SyntaxError:
                    errors.append(f"{prefix}:unparsable_python")
                else:
                    callable_names = {
                        node.name
                        for node in ast.walk(tree)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }
                    if ref.symbol not in callable_names:
                        errors.append(f"{prefix}:missing_symbol")
            elif path.suffix in {".js", ".mjs", ".ts", ".tsx"}:
                if not re.search(
                    rf"(?:export\s+)?(?:async\s+)?function\s+{re.escape(ref.symbol)}\s*\(",
                    source,
                ):
                    errors.append(f"{prefix}:missing_symbol")
            elif path.suffix == ".sh":
                if not re.search(
                    rf"^\s*(?:function\s+)?{re.escape(ref.symbol)}\s*\(\)\s*\{{",
                    source,
                    re.MULTILINE,
                ):
                    errors.append(f"{prefix}:missing_symbol")
            else:
                errors.append(f"{prefix}:symbol_not_callable_source")
    return errors


def _validate_test_id(test_id: str, *, row_id: str, polarity: str) -> list[str]:
    """Verify pytest-like ``path::...::test_name`` IDs without importing tests."""

    parts = test_id.split("::")
    prefix = f"matrix_{polarity}_test:{row_id}:{test_id}"
    if len(parts) < 2 or not parts[-1].startswith("test_"):
        return [f"{prefix}:invalid_id"]
    path = _safe_repo_path(parts[0])
    if path is None or path.suffix != ".py":
        return [f"{prefix}:unsafe_or_non_python_path"]
    if not path.is_file():
        return [f"{prefix}:missing_path"]
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"{prefix}:unreadable:{type(exc).__name__}"]
    pattern = re.compile(
        rf"^\s*(?:async\s+)?def\s+{re.escape(parts[-1])}\s*\(",
        re.MULTILINE,
    )
    if not pattern.search(source):
        return [f"{prefix}:missing_test"]
    return []


def _row_refs(row: MatrixRow) -> Iterable[SourceRef]:
    yield from row.authority
    yield from row.production_owners
    yield from row.dynamic_gates
    for binding in row.prompts:
        yield binding.renderer
        yield from binding.templates


def _renderer_callable_source(renderer: SourceRef) -> str | None:
    """Return one renderer function's source, without importing its module.

    Matrix validation needs to prove that each listed role still injects the
    shared, source-owned strict-runtime overlay.  Looking for the symbol in the
    whole module is too weak: another renderer could retain the call while the
    listed renderer silently loses it.  Restrict the check to the named
    function's AST segment instead.
    """

    path = _safe_repo_path(renderer.path)
    if path is None or not path.is_file() or not renderer.symbol:
        return None
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == renderer.symbol:
            return ast.get_source_segment(source, node)
    return None


def _strict_runtime_prompt_overlay() -> tuple[str | None, str | None]:
    """Load the exact common overlay that all current role renderers inject.

    This is a source-only quality check: the overlay is stdlib-only and has no
    runtime side effects.  A failed import or empty result is an explicit matrix
    failure, never a reason to treat role-specific template text as equivalent.
    """

    try:
        from strategy_reference_pack import current_strict_runtime_prompt_overlay

        overlay = current_strict_runtime_prompt_overlay()
    except Exception as exc:  # fail closed even if a future overlay adds imports
        return None, type(exc).__name__
    if not isinstance(overlay, str) or not overlay.strip():
        return None, "empty"
    return overlay, None


def _prompt_material(binding: PromptBinding, overlay: str) -> str | None:
    """Return the checked-in template inputs plus their common live overlay."""

    template_text: list[str] = []
    for template in binding.templates:
        path = _safe_repo_path(template.path)
        if path is None or not path.is_file():
            return None
        try:
            template_text.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return None
    return "\n\n".join((*template_text, overlay))


def validate_alignment_matrix(
    rows: Sequence[MatrixRow] = CURRENT_ALIGNMENT_ROWS,
) -> list[str]:
    """Return deterministic fail-closed errors for a matrix source snapshot.

    Validation intentionally does not execute evolution, certification, or a
    match.  It proves that a current row cannot silently devolve into a stale
    prose assertion whose listed owner, gate, prompt, or regression no longer
    exists.  Runtime-pending state is represented explicitly, never guessed.
    """

    errors: list[str] = []
    seen_ids: set[str] = set()
    coverage: set[str] = set()
    overlay, overlay_error = _strict_runtime_prompt_overlay()
    if overlay_error is not None:
        errors.append(f"matrix_prompt_overlay_unavailable:{overlay_error}")
    for row in rows:
        if not _RULE_ID_RE.fullmatch(row.rule_id):
            errors.append(f"matrix_rule_id_invalid:{row.rule_id}")
        if row.rule_id in seen_ids:
            errors.append(f"matrix_rule_id_duplicate:{row.rule_id}")
        seen_ids.add(row.rule_id)
        if row.status not in _VALID_STATUSES:
            errors.append(f"matrix_status_invalid:{row.rule_id}:{row.status}")
        if row.evidence_state not in _VALID_EVIDENCE_STATES:
            errors.append(
                f"matrix_evidence_state_invalid:{row.rule_id}:{row.evidence_state}"
            )
        if row.status == CURRENT_STATUS and row.evidence_state == HISTORICAL:
            errors.append(f"matrix_current_row_is_historical:{row.rule_id}")
        if row.status == SUPERSEDED_STATUS and not row.historical_reason.strip():
            errors.append(f"matrix_superseded_reason_missing:{row.rule_id}")
        if not row.coverage:
            errors.append(f"matrix_coverage_missing:{row.rule_id}")
        coverage.update(row.coverage)
        required_collections = (
            ("authority", row.authority),
            ("production_owners", row.production_owners),
            ("dynamic_gates", row.dynamic_gates),
            ("prompts", row.prompts),
            ("positive_tests", row.positive_tests),
            ("negative_tests", row.negative_tests),
        )
        for name, values in required_collections:
            if not values:
                errors.append(f"matrix_{name}_missing:{row.rule_id}")
        if "→" not in row.producer_consumer or len(row.producer_consumer.strip()) < 12:
            errors.append(f"matrix_producer_consumer_invalid:{row.rule_id}")
        if len(row.fail_closed.strip()) < 24:
            errors.append(f"matrix_fail_closed_missing:{row.rule_id}")

        for ref in row.authority:
            errors.extend(_validate_ref(ref, row_id=row.rule_id, field="authority"))
        for ref in row.production_owners:
            errors.extend(_validate_ref(ref, row_id=row.rule_id, field="owner"))
        for ref in row.dynamic_gates:
            errors.extend(
                _validate_ref(
                    ref,
                    row_id=row.rule_id,
                    field="dynamic_gate",
                    require_symbol=True,
                )
            )
        for binding in row.prompts:
            if not binding.role.strip():
                errors.append(f"matrix_prompt_role_missing:{row.rule_id}")
            errors.extend(
                _validate_ref(
                    binding.renderer,
                    row_id=row.rule_id,
                    field="prompt_renderer",
                    require_symbol=True,
                )
            )
            if not binding.templates:
                errors.append(f"matrix_prompt_template_missing:{row.rule_id}:{binding.role}")
            for template in binding.templates:
                errors.extend(
                    _validate_ref(
                        template,
                        row_id=row.rule_id,
                        field="prompt_template",
                    )
                )
        if row.status == CURRENT_STATUS:
            supplied_roles = {binding.role for binding in row.prompts}
            missing_roles = sorted(REQUIRED_PROMPT_ROLES - supplied_roles)
            if missing_roles:
                errors.append(
                    f"matrix_current_prompt_roles_missing:{row.rule_id}:"
                    + ",".join(missing_roles)
                )
            if not row.prompt_statement.strip():
                errors.append(f"matrix_prompt_statement_missing:{row.rule_id}")
            if not row.prompt_required_terms:
                errors.append(f"matrix_prompt_required_terms_missing:{row.rule_id}")
            normalized_statement = row.prompt_statement.casefold()
            seen_prompt_terms: set[str] = set()
            for raw_term in row.prompt_required_terms:
                term = raw_term.strip()
                normalized_term = term.casefold()
                if not term:
                    errors.append(f"matrix_prompt_required_term_blank:{row.rule_id}")
                    continue
                if normalized_term in seen_prompt_terms:
                    errors.append(
                        f"matrix_prompt_required_term_duplicate:{row.rule_id}:{term}"
                    )
                    continue
                seen_prompt_terms.add(normalized_term)
                if normalized_term not in normalized_statement:
                    errors.append(
                        f"matrix_prompt_statement_term_missing:{row.rule_id}:{term}"
                    )
            if overlay is not None:
                for binding in row.prompts:
                    renderer_source = _renderer_callable_source(binding.renderer)
                    if renderer_source is None or not re.search(
                        r"\bcurrent_strict_runtime_prompt_overlay\s*\(",
                        renderer_source,
                    ):
                        errors.append(
                            "matrix_prompt_renderer_overlay_missing:"
                            f"{row.rule_id}:{binding.role}"
                        )
                    material = _prompt_material(binding, overlay)
                    if material is None:
                        continue
                    normalized_material = material.casefold()
                    for raw_term in row.prompt_required_terms:
                        term = raw_term.strip()
                        if term and term.casefold() not in normalized_material:
                            errors.append(
                                "matrix_prompt_rendered_term_missing:"
                                f"{row.rule_id}:{binding.role}:{term}"
                            )
        for test_id in row.positive_tests:
            errors.extend(_validate_test_id(test_id, row_id=row.rule_id, polarity="positive"))
        for test_id in row.negative_tests:
            errors.extend(_validate_test_id(test_id, row_id=row.rule_id, polarity="negative"))

        if row.status == CURRENT_STATUS:
            for ref in _row_refs(row):
                if _is_archive_path(ref.path):
                    errors.append(
                        f"matrix_current_archive_reference:{row.rule_id}:{ref.display()}"
                    )
            for test_id in (*row.positive_tests, *row.negative_tests):
                path = test_id.split("::", 1)[0]
                if _is_archive_path(path):
                    errors.append(
                        f"matrix_current_archive_test_reference:{row.rule_id}:{test_id}"
                    )
            for field, text in (
                ("producer_consumer", row.producer_consumer),
                ("fail_closed", row.fail_closed),
                ("historical_reason", row.historical_reason),
                ("prompt_statement", row.prompt_statement),
            ):
                normalized = str(text).replace("\\", "/").lower()
                if "archive/" in normalized or "docs/archive" in normalized:
                    errors.append(
                        f"matrix_current_archive_text_reference:{row.rule_id}:{field}"
                    )

    missing_coverage = sorted(REQUIRED_COVERAGE - coverage)
    errors.extend(f"matrix_required_coverage_missing:{item}" for item in missing_coverage)

    handshake_rows = [
        row for row in rows if row.rule_id == _RAW_NAME_HANDSHAKE_RULE_ID
    ]
    if len(handshake_rows) != 1:
        errors.append(
            "matrix_raw_name_handshake_rule_missing_or_ambiguous:"
            f"count={len(handshake_rows)}"
        )
    else:
        handshake = handshake_rows[0]
        if handshake.status != CURRENT_STATUS or handshake.evidence_state != SOURCE_CONTRACT:
            errors.append("matrix_raw_name_handshake_not_current_source_contract")
        if "raw_tcp_name_handshake" not in handshake.coverage:
            errors.append("matrix_raw_name_handshake_coverage_missing")
        if not handshake.prompt_statement.strip():
            errors.append("matrix_raw_name_handshake_prompt_statement_missing")
        body = " ".join((
            handshake.prompt_statement,
            handshake.producer_consumer,
            handshake.fail_closed,
        )).lower()
        for term in _RAW_NAME_HANDSHAKE_REQUIRED_TERMS:
            if term not in body:
                errors.append(f"matrix_raw_name_handshake_semantics_missing:{term}")
        for test_id in sorted(
            _RAW_NAME_HANDSHAKE_REQUIRED_POSITIVE_TESTS
            - set(handshake.positive_tests)
        ):
            errors.append(f"matrix_raw_name_handshake_positive_missing:{test_id}")
        for test_id in sorted(
            _RAW_NAME_HANDSHAKE_REQUIRED_NEGATIVE_TESTS
            - set(handshake.negative_tests)
        ):
            errors.append(f"matrix_raw_name_handshake_negative_missing:{test_id}")

    quality_rows = [
        row for row in rows if row.rule_id == _QUALITY_RUNTIME_IDENTITY_RULE_ID
    ]
    if len(quality_rows) != 1:
        errors.append(
            "matrix_quality_runtime_identity_rule_missing_or_ambiguous:"
            f"count={len(quality_rows)}"
        )
    else:
        quality = quality_rows[0]
        if quality.status != CURRENT_STATUS or quality.evidence_state != SOURCE_CONTRACT:
            errors.append("matrix_quality_runtime_identity_not_current_source_contract")
        owner_symbols = {ref.display() for ref in quality.production_owners}
        for owner in sorted(
            set(_QUALITY_RUNTIME_IDENTITY_REQUIRED_OWNER_SYMBOLS) - owner_symbols
        ):
            errors.append(f"matrix_quality_runtime_identity_owner_missing:{owner}")
        body = " ".join((
            quality.prompt_statement,
            quality.producer_consumer,
            quality.fail_closed,
        )).casefold()
        for term in _QUALITY_RUNTIME_IDENTITY_REQUIRED_TERMS:
            if term.casefold() not in body:
                errors.append(
                    f"matrix_quality_runtime_identity_semantics_missing:{term}"
                )
        for test_id in sorted(
            set(_QUALITY_RUNTIME_IDENTITY_REQUIRED_POSITIVE_TESTS)
            - set(quality.positive_tests)
        ):
            errors.append(
                f"matrix_quality_runtime_identity_positive_missing:{test_id}"
            )
        for test_id in sorted(
            set(_QUALITY_RUNTIME_IDENTITY_REQUIRED_NEGATIVE_TESTS)
            - set(quality.negative_tests)
        ):
            errors.append(
                f"matrix_quality_runtime_identity_negative_missing:{test_id}"
            )
    return sorted(set(errors))


def render_current_matrix_markdown(
    rows: Sequence[MatrixRow] = CURRENT_ALIGNMENT_ROWS,
) -> str:
    """Render the checked-in current view used by the human matrix document."""

    lines = [
        "<!-- executable-national-alignment-matrix:begin -->",
        "## Executable current-contract registry (generated)",
        "",
        "This block is generated from `web/core/national_alignment_matrix.py` "
        f"(schema {MATRIX_SCHEMA_VERSION}) and is regression-checked.  `source_contract` "
        "verifies source paths/symbols/test anchors only; `runtime_pending` is not a "
        "runtime, certificate, or strength claim. `current` means an active requirement, "
        "and only `superseded` rows may point at historical archive material.",
        "",
    ]
    for row in rows:
        lines.extend((
            f"### `{row.rule_id}` — {row.status} / {row.evidence_state}",
            "",
            "- Authority/source: " + "; ".join(f"`{ref.display()}`" for ref in row.authority),
            "- Production owner: " + "; ".join(
                f"`{ref.display()}`" for ref in row.production_owners
            ),
            "- Dynamic gate: " + "; ".join(
                f"`{ref.display()}`" for ref in row.dynamic_gates
            ),
            "- Prompt renderer/template: " + "; ".join(
                f"{binding.role}=`{binding.renderer.display()}` → "
                + ", ".join(f"`{template.display()}`" for template in binding.templates)
                for binding in row.prompts
            ),
            *(
                (f"- Prompt statement: {row.prompt_statement}",)
                if row.prompt_statement
                else ()
            ),
            f"- Producer → consumer: {row.producer_consumer}",
            "- Positive regression: " + "; ".join(f"`{value}`" for value in row.positive_tests),
            "- Negative regression: " + "; ".join(f"`{value}`" for value in row.negative_tests),
            f"- Fail-closed: {row.fail_closed}",
        ))
        if row.historical_reason:
            lines.append(f"- Superseded reason: {row.historical_reason}")
        lines.append("")
    lines.append("<!-- executable-national-alignment-matrix:end -->")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - operator/doc helper
    issues = validate_alignment_matrix()
    if issues:
        raise SystemExit("\n".join(issues))
    print(render_current_matrix_markdown(), end="")
