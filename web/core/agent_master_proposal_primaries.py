"""Master proposal-primaries companion.

Extracted from agent_master_validation.py as a single business responsibility:
the closed ``proposal primaries`` cluster.  This module owns the falsifier
primary mapping, the canonical/architecture primaries projection, the compact
falsifier mapping text shown to Scout prompts, the closed proposal JSON shape
skeleton, and the cross-binding ``mechanism_target`` validation (the file's
largest single function).

The parent module owns the proposal schema constants and the larger
proposal-packet symbol graph; this companion reaches them via ``_amv.<name>``.
Intra-cluster calls stay bare.  All public symbols are re-exported by
agent_master_validation.py (as thin delegate shells) and then by
agent_master.py for backward compatibility.
"""

from __future__ import annotations

import json
import re

import agent_master_validation as _amv


def _proposal_falsifier_primary(test_name: object) -> str | None:
    """Return the closed state-learning primary for one typed falsifier."""

    return _amv.MASTER_PROPOSAL_FALSIFIER_PRIMARY.get(str(test_name or "").strip())


def _canonical_proposal_primaries(
    values: object,
) -> tuple[str, ...] | None:
    """Normalize an optional frozen set of permitted proposal primaries."""

    if values is None:
        return None
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise ValueError("proposal allowed primaries must be a collection")
    known = set(_amv.STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS)
    normalized = tuple(sorted({
        str(value).strip()
        for value in values
        if str(value).strip()
    }))
    if not normalized:
        return None
    if any(value not in known for value in normalized):
        raise ValueError("proposal allowed primaries are invalid")
    return normalized


def _architecture_proposal_primaries(
    architecture_policy: dict | None,
) -> tuple[str, ...] | None:
    """Derive Scout-visible primaries from immutable architecture checks.

    This is deliberately a projection of the system policy, not an LLM choice.
    If the policy has no falsifier-mapped deficit we preserve the historic
    all-card view.  A focused policy receives only matching cards/mapping rows,
    preventing cross-axis examples from leaking into the sole schema retry.
    """

    if not isinstance(architecture_policy, dict):
        return None
    required_checks = list(architecture_policy.get("plan_required_floor_checks") or ())
    focus = architecture_policy.get("selected_focus")
    if isinstance(focus, dict):
        required_checks.extend(focus.get("required_checks") or ())
    required_set = {str(check).strip() for check in required_checks if str(check).strip()}
    primaries = tuple(
        primary
        for _test, primary in _amv.MASTER_PROPOSAL_FALSIFIER_PRIMARY.items()
        if _test in required_set
    )
    return _canonical_proposal_primaries(primaries) if primaries else None


def _proposal_falsifier_mapping_text(
    *,
    allowed_primaries: tuple[str, ...] | None = None,
) -> str:
    """Render the compact machine mapping needed by proposal Scouts.

    Aliases, derived quality checks, and final Worker prompt terms remain
    system-owned validator/compiler data.  Repeating them in every independent
    Scout prompt added thousands of characters without creating provider-owned
    output fields.
    """

    allowed = _canonical_proposal_primaries(allowed_primaries)
    rows = {
        test_name: {
            "state_learning_primary": primary,
            "mechanism_target": (
                _amv.STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
            ),
            "intervention_target": (
                _amv.STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
            ),
        }
        for test_name, primary in _amv.MASTER_PROPOSAL_FALSIFIER_PRIMARY.items()
        if allowed is None or primary in allowed
    }
    if not rows:
        # An over-narrow architecture-policy filter (e.g. a singleton
        # no-strength bootstrap) can exclude every falsifier primary.  The Scout
        # still needs a complete mapping table to choose a valid test_name, so
        # fall back to the full unfiltered table rather than crashing the whole
        # renderer and abandoning the generation.
        rows = {
            test_name: {
                "state_learning_primary": primary,
                "mechanism_target": (
                    _amv.STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
                ),
                "intervention_target": (
                    _amv.STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
                ),
            }
            for test_name, primary in _amv.MASTER_PROPOSAL_FALSIFIER_PRIMARY.items()
        }
    return json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _proposal_closed_json_shape() -> str:
    """Return the exact proposal key shape shown to every Scout.

    The runtime validator remains the authority.  This compact machine-readable
    skeleton prevents a frequent provider mistake where the top-level typed
    ``mechanism_target`` is redundantly copied into the closed ``falsifier``
    object, consuming the single schema-repair attempt.
    """

    return json.dumps({
        "targeted_failure": "<text>",
        "structural_change": "<text>",
        "counterfactual": "<text>",
        "measurement": "<exact measurement contract>",
        "why_not_threshold_tuning": "<text>",
        "mechanism_target": "<exact mapping target>",
        "target_files": ["policy.py"],
        "expected_diff": "<text>",
        "source_symbols": ["<file.py:symbol>"],
        "change_symbol": "<file.py:callee>",
        "reachable_chain": ["<file.py:caller>", "<file.py:callee>"],
        "falsifier": {
            "test_name": "<one allowed test>",
            "state_learning_primary": "<mapped primary>",
            "intervention_target": "<same mapping target>",
            "control": "<text>",
            "intervention": "<text>",
            "expected_observation": "<text>",
        },
        "evidence_refs": ["source:<file.py:symbol>"],
        "risks": "<text>",
    }, ensure_ascii=False, separators=(",", ":"))


def _proposal_mechanism_target_errors(
    proposal: dict,
    falsifier: dict,
) -> tuple[str, ...]:
    """Cross-bind the typed mechanism target to the executable proposal fields."""

    primary = _proposal_falsifier_primary(falsifier.get("test_name"))
    if primary is None:
        return ("proposal_mechanism_target_primary_invalid",)
    expected = _amv.STATE_LEARNING_PRIMARY_INTERVENTION_TARGETS[primary]
    declared = proposal.get("mechanism_target")
    intervention_target = falsifier.get("intervention_target")
    errors: list[str] = []
    if declared != expected:
        errors.append(
            f"proposal_mechanism_target_mismatch:expected={expected}:actual={declared}"
        )
    if intervention_target != expected:
        errors.append(
            "proposal_falsifier_intervention_target_mismatch:"
            f"expected={expected}:actual={intervention_target}"
        )
    executable_fields = {
        "structural_change": proposal.get("structural_change"),
        "expected_diff": proposal.get("expected_diff"),
        "intervention": falsifier.get("intervention"),
    }

    def literal_appears(value: object, literal: str) -> bool:
        if not isinstance(value, str):
            return False
        # The required dot literal may prefix a qualified child, but must not
        # pass as a substring of a different identifier (for example
        # ``opponent.rates_backup``).
        pattern = (
            r"(?<![a-z0-9_])"
            + re.escape(literal)
            + r"(?![a-z0-9_])"
        )
        return re.search(pattern, value, flags=re.IGNORECASE) is not None

    missing_target_fields = sorted(
        field
        for field, value in executable_fields.items()
        if not literal_appears(value, expected)
    )
    if missing_target_fields:
        errors.append(
            "proposal_mechanism_target_missing_from_executable_fields:"
            + expected
            + ":"
            + ",".join(missing_target_fields)
        )
    mechanism_text = " ".join(
        value.lower()
        for value in executable_fields.values()
        if isinstance(value, str)
    )

    def mask_literals(text: str, literals: tuple[str, ...] | list[str]) -> str:
        masked = text
        for literal in sorted(set(literals), key=len, reverse=True):
            patterns = [(
                r"(?<![a-z0-9_])"
                + re.escape(literal.lower())
                + r"(?![a-z0-9_])"
            )]
            parts = literal.lower().split(".")
            if len(parts) >= 2 and all(
                re.fullmatch(r"[a-z0-9_]+", part) for part in parts
            ):
                patterns.append(
                    r"(?<![a-z0-9_])(?:context|decision_context)"
                    + "".join(
                        r"\s*\[\s*['\"]"
                        + re.escape(part)
                        + r"['\"]\s*\]"
                        for part in parts
                    )
                    + r"(?![a-z0-9_])"
                )
            for pattern in patterns:
                masked = re.sub(
                    pattern,
                    " ",
                    masked,
                    flags=re.IGNORECASE,
                )
        return masked

    root_scoped_list_errors: list[str] = []
    expected_root_children = {
        alias.rsplit(".", 1)[1]
        for alias in _amv.STATE_LEARNING_INTERVENTION_TARGET_ALIASES[expected]
        if alias.startswith(expected + ".")
        and re.fullmatch(r"[a-z_][a-z0-9_]*", alias.rsplit(".", 1)[1])
    }

    def mask_root_scoped_shared_leaves(text: str) -> str:
        """Mask a closed, root-qualified shorthand list for the expected axis.

        The proposal contract normally requires a full owner-qualified child
        literal.  A Scout can also make that ownership unambiguous with the
        deliberately narrow ``opponent.rates (aggression, fold_to_raise)``
        notation: the exact selectable root is immediately followed by a flat
        list of identifier leaves.  Treat that syntax as qualified rather than
        rejecting it as a bare shared leaf.  A short natural-language connector
        such as ``opponent.rates root (aggression, fold_to_raise)`` or
        ``opponent.rates profile (aggression, fold_to_raise)`` is also accepted:
        the connector words do not change ownership and the parenthesized list
        is still the explicit child set.  Do not accept prose, nested paths,
        values, or a different root inside the parentheses; those remain
        fail-closed and are still scanned for foreign targets below.
        """

        # Allow up to three short alphabetic connector words (e.g. "root",
        # "profile", "values") between the root literal and the opening paren.
        # Dots, digits, underscores, or longer identifiers in the connector
        # position are rejected so a different qualified target cannot pose as
        # a root-scoped list header.
        root_pattern = re.compile(
            r"(?<![a-z0-9_])"
            + re.escape(expected)
            + r"((?:\s+[a-z]{1,20}){0,3})\s*\(([^()]*)\)",
            flags=re.IGNORECASE,
        )

        def replace(match: re.Match[str]) -> str:
            connector = match.group(1) or ""
            body = match.group(2)
            fields = re.split(r"\s*(?:,|\band\b)\s*", body)
            normalized_fields = [
                field.strip().strip("`'\"").lower()
                for field in fields
            ]
            if not normalized_fields or any(
                re.fullmatch(r"[a-z_][a-z0-9_]*", field) is None
                for field in normalized_fields
            ):
                return match.group(0)
            unknown_fields = sorted(set(normalized_fields) - expected_root_children)
            if unknown_fields:
                root_scoped_list_errors.extend(
                    "proposal_mechanism_root_scoped_unknown_leaf:"
                    + expected
                    + ":"
                    + field
                    for field in unknown_fields
                )
                return match.group(0)
            masked_body = body
            for leaf, owners in _amv.STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS.items():
                if (
                    leaf.lower() in normalized_fields
                    and f"{expected}.{leaf}" in owners
                ):
                    masked_body = re.sub(
                        r"(?<![a-z0-9_])" + re.escape(leaf) + r"(?![a-z0-9_])",
                        " ",
                        masked_body,
                        flags=re.IGNORECASE,
                    )
            # Replace the body in-place; also blank the connector words so the
            # downstream unowned-text scan cannot re-introduce a stray token
            # (defensive: connector words are not leaves, but keep the masked
            # text clean).
            masked_match = match.group(0).replace(body, masked_body, 1)
            if connector:
                masked_match = masked_match.replace(connector, " ", 1)
            return masked_match

        return root_pattern.sub(replace, text)

    # A SCREAMING_SNAKE_CASE token in executable prose is a Python source
    # constant reference (e.g. ``FOLD_TO_RAISE_PRIOR``), not a bare shared
    # leaf.  Masking it before lower-casing keeps its normalized form
    # (``fold_to_raise_prior``) from containing the bounded shared-leaf
    # substring (``_fold_to_raise_``).  A token whose lowercase form is itself
    # a known leaf or alias is left intact, so an all-uppercase bare leaf
    # (``FOLD_TO_RAISE``) is still caught below.
    _screaming_protected = {
        leaf.lower() for leaf in _amv.STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS
    }
    _screaming_protected |= {
        alias.lower()
        for aliases in _amv.STATE_LEARNING_INTERVENTION_TARGET_ALIASES.values()
        for alias in aliases
    }

    def _mask_screaming_constants(text: str) -> str:
        def _replace(match: re.Match[str]) -> str:
            token = match.group(0)
            return " " if token.lower() not in _screaming_protected else token

        return re.sub(
            r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{2,}(?![A-Za-z0-9_])",
            _replace,
            text,
        )

    ambiguous_shared_leaves: list[str] = []
    unowned_fields = []
    for value in executable_fields.values():
        if not isinstance(value, str):
            continue
        masked_value = mask_literals(
            _mask_screaming_constants(value).lower(),
            [
                owner
                for owners in _amv.STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS.values()
                for owner in owners
            ],
        )
        masked_value = mask_root_scoped_shared_leaves(masked_value)
        unowned_fields.append(masked_value)
    unowned_mechanism_text = " ".join(unowned_fields)
    for leaf, owners in _amv.STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS.items():
        unowned_text = unowned_mechanism_text
        # Outside a validated root-scoped list, preserve every spelling of a
        # shared leaf.  Executable prose cannot distinguish an explanatory
        # phrase from a second, unowned input; treating either as harmless
        # would let another opponent namespace alter the claimed mechanism.
        normalized_unowned = re.sub(
            r"[^a-z0-9]+", "_", unowned_text
        ).strip("_")
        bounded_unowned = f"_{normalized_unowned}_"
        compact_leaf = re.sub(r"[^a-z0-9]+", "", leaf.lower())
        if (
            f"_{leaf.lower()}_" in bounded_unowned
            or re.search(
                r"(?<![a-z0-9])"
                + re.escape(compact_leaf)
                + r"(?![a-z0-9])",
                unowned_text,
            )
        ):
            ambiguous_shared_leaves.append(leaf)
    errors.extend(sorted(set(root_scoped_list_errors)))
    if ambiguous_shared_leaves:
        errors.append(
            "proposal_mechanism_shared_leaf_requires_full_namespace:"
            + ",".join(sorted(ambiguous_shared_leaves))
        )

    # Mask complete qualified fields owned by the expected axis before looking
    # for foreign aliases. Otherwise a legitimate phrase such as
    # ``opponent.terminal_response.fold_to_raise rate`` contains the token
    # sequence ``raise rate`` and can be misclassified as the action-profile
    # alias ``raise_rate``.
    expected_qualified_aliases = tuple(
        alias
        for alias in _amv.STATE_LEARNING_INTERVENTION_TARGET_ALIASES[expected]
        if alias.startswith(expected + ".")
    )
    qualified_identifier_continuations = sorted(
        f"{alias}:{field}"
        for field, value in executable_fields.items()
        if isinstance(value, str)
        for alias in expected_qualified_aliases
        if re.search(
            r"(?<![a-z0-9_])"
            + re.escape(alias)
            + r"(?=[a-z0-9_])",
            value,
            flags=re.IGNORECASE,
        )
    )
    if qualified_identifier_continuations:
        errors.append(
            "proposal_mechanism_qualified_target_identifier_continuation:"
            + ",".join(qualified_identifier_continuations)
        )
    foreign_scan_text = mask_literals(mechanism_text, expected_qualified_aliases)

    def alias_appears(alias: str) -> bool:
        parts = re.findall(r"[a-z0-9]+", alias.lower())
        if not parts:
            return False
        joiner = (
            r"[_]*"
            if alias.lower() in _amv._PROSE_PRONE_ALIASES
            else r"[^a-z0-9]*"
        )
        alias_pattern = joiner.join(map(re.escape, parts))
        if re.search(
            r"(?<![a-z0-9_])"
            + alias_pattern
            + r"(?![a-z0-9_])",
            foreign_scan_text,
        ):
            return True
        # Long closed aliases must also fail closed when identifier characters
        # are appended (``terminalresponsebackup``).  Keep a leading boundary
        # that also rejects an underscore prefix, so a longer local identifier
        # such as ``raise_fold_rate`` is not misread as the ``fold_rate`` alias;
        # keep short lexical terms such as ``donk`` boundary-only so words such
        # as ``interactionprofile`` and ``donkey`` remain legal.
        compact_alias = "".join(parts)
        if len(compact_alias) < 8:
            return False
        return re.search(
            r"(?<![a-z0-9_])" + alias_pattern,
            foreign_scan_text,
        ) is not None
    # ``deadline`` is a universal safety boundary and can legitimately appear
    # in every bounded strategy proposal.  All other closed mechanism axes have
    # narrow aliases so a proposal cannot carry the correct typed label while
    # its executable prose actually varies terminal, range, or line state.
    foreign_targets = {
        target
        for target, aliases in _amv.STATE_LEARNING_INTERVENTION_TARGET_ALIASES.items()
        if target not in {expected, "deadline"}
        and any(alias_appears(alias) for alias in aliases)
    }

    # ``opponent.samples.fold_to_raise`` shares a leaf with two independently
    # governed decision inputs but is not itself a selectable primary target.
    # It must still be treated as foreign executable state, including when a
    # proposal claims it is unchanged.  Otherwise a valid action-profile label
    # could smuggle an unreviewed sample-count intervention through a shared
    # leaf that is absent from the selectable-target alias table.
    def owner_appears(owner: str) -> bool:
        patterns = [
            r"(?<![a-z0-9_])"
            + re.escape(owner.lower())
            + r"(?![a-z0-9_])"
        ]
        parts = owner.lower().split(".")
        if len(parts) >= 2 and all(
            re.fullmatch(r"[a-z0-9_]+", part) for part in parts
        ):
            patterns.append(
                r"(?<![a-z0-9_])(?:context|decision_context)"
                + "".join(
                    r"\s*\[\s*['\"]"
                    + re.escape(part)
                    + r"['\"]\s*\]"
                    for part in parts
                )
                + r"(?![a-z0-9_])"
            )
        return any(
            re.search(pattern, mechanism_text, flags=re.IGNORECASE) is not None
            for pattern in patterns
        )

    for owners in _amv.STATE_LEARNING_SHARED_INTERVENTION_LEAF_OWNERS.values():
        for owner in owners:
            owner_target = owner.rsplit(".", 1)[0]
            if owner_target != expected and owner_appears(owner):
                foreign_targets.add(owner_target)
    foreign_targets = sorted(foreign_targets)
    if foreign_targets:
        errors.append(
            "proposal_mechanism_foreign_targets_in_executable_claim:"
            + ",".join(foreign_targets)
        )
    return tuple(errors)
