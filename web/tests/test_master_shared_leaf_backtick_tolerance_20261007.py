"""Root-scoped shared-leaf whitelist must tolerate decoration (2026-10-07).

The v516-v524 proposal packets were killed nine consecutive times by
``proposal_mechanism_shared_leaf_requires_full_namespace:fold_to_raise`` even
though the Scout output was semantically the exact root-scoped list the prompt
teaches (``opponent.rates (aggression, fold_to_raise)``): Markdown backticks
around the root or the list items, and separator spellings of a leaf inside
the list (``fold to raise`` / ``fold-to-raise``), broke the whitelist regex —
not the namespace contract.  A bare shared leaf outside any root-scoped list
must keep failing closed.
"""

import sys
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


TARGET = "opponent.rates"
SHARED_LEAF_ERROR = (
    "proposal_mechanism_shared_leaf_requires_full_namespace:fold_to_raise"
)


def _fields(scoped: str) -> tuple[dict, dict]:
    """One action-profile proposal whose three executable fields all carry
    the same root-scoped spelling under test."""

    return (
        {
            "mechanism_target": TARGET,
            "structural_change": (
                f"Route only {scoped} through the bounded live decision consumer."
            ),
            "expected_diff": (
                f"The paired typed intent changes only when {scoped} changes."
            ),
        },
        {
            "test_name": "incremental_opponent_model",
            "intervention_target": TARGET,
            "intervention": (
                f"Change only {scoped} in the paired decision context."
            ),
        },
    )


def _errors(scoped: str) -> tuple[str, ...]:
    import agent_master_proposal_primaries as pp

    proposal, falsifier = _fields(scoped)
    return pp._proposal_mechanism_target_errors(proposal, falsifier)


@pytest.mark.parametrize(
    "scoped",
    (
        # Undecorated canonical form (already legal; guards regression).
        "opponent.rates (aggression, fold_to_raise)",
        # Real v522/v524 output: root wrapped in backticks, items bare.
        "`opponent.rates` (aggression, fold_to_raise)",
        # Root and every list item wrapped in backticks.
        "`opponent.rates` (`aggression`, `fold_to_raise`)",
        # Single/double quotes decorate the same list.
        "'opponent.rates' ('aggression', 'fold_to_raise')",
        "\"opponent.rates\" (\"aggression\", \"fold_to_raise\")",
        # Separator spellings of the leaf inside its root-scoped list.
        "opponent.rates (aggression, fold to raise)",
        "opponent.rates (aggression, fold-to-raise)",
        # Decorated root plus separator-spelled leaf together.
        "`opponent.rates` (aggression, fold-to-raise)",
    ),
)
def test_root_scoped_list_accepts_decorated_and_separator_spellings(scoped):
    errors = _errors(scoped)
    assert errors == (), (
        f"semantically qualified root-scoped list rejected: {scoped!r} -> {errors}"
    )


def test_bare_shared_leaf_outside_any_list_still_rejected():
    """Form 3 — a shared leaf (even backticked) with no root-scoped list
    around it is a genuine namespace violation and must stay rejected."""

    errors = _errors(
        "Read the opponent.rates root byte-identically and expose "
        "`fold_to_raise` posterior outputs."
    )
    assert SHARED_LEAF_ERROR in errors, (
        f"bare shared leaf outside a root-scoped list must fail closed: {errors}"
    )


def test_separator_spelled_shared_leaf_as_prose_still_rejected():
    """A separator-spelled leaf followed by more words inside the
    parentheses is prose, not a flat leaf list; the list must not excuse it."""

    errors = _errors("opponent.rates (aggression, fold to raise tendency)")
    assert SHARED_LEAF_ERROR in errors, (
        "prose inside the parentheses must not qualify the shared leaf: "
        f"{errors}"
    )


def test_unknown_identifier_leaf_still_rejected():
    """Decoration tolerance must not widen the closed child whitelist."""

    errors = _errors("opponent.rates (aggression, tempo)")
    assert (
        "proposal_mechanism_root_scoped_unknown_leaf:opponent.rates:tempo"
        in errors
    ), f"unknown child leaf accepted: {errors}"


def test_leaf_free_field_does_not_regress():
    """A field naming only the root (no leaf at all) keeps passing."""

    errors = _errors("opponent.rates through the bounded live decision consumer")
    assert errors == (), f"leaf-free field rejected: {errors}"


def test_shared_leaf_repair_guidance_matches_the_accepted_template():
    """The retry guidance must teach the exact form the prompt accepts —
    previously it demanded 'the selected root literal only', contradicting
    the prompt-recognized root-scoped list."""
    import agent_master_validation as amv

    guidance = amv._proposal_schema_repair_guidance(
        (SHARED_LEAF_ERROR,),
        require_snapshot_evidence=False,
        allowed_primaries=("action_profile",),
    )
    assert "opponent.rates (aggression, fold_to_raise)" in guidance, (
        "the shared-leaf repair guidance must show the accepted root-scoped "
        f"list template verbatim; got: {guidance!r}"
    )
    assert "`opponent.rates" not in guidance, (
        "the template must be undecorated so the retry cannot copy backticks: "
        f"{guidance!r}"
    )


# --- Adversarial-audit regressions (2026-10-07, /tmp/audit/attack*.py) --------


@pytest.mark.parametrize(
    "scoped",
    (
        # Quote decoration glued to identifier characters on one or both
        # sides: the strip must never manufacture a new identifier adjacency
        # (x'fold_to_raise'y -> xfold_to_raisey escaped the scan).
        "opponent.rates root and x'fold_to_raise'y posterior",
        "opponent.rates root and q'fold_to_raise' posterior",
        "opponent.rates root and f'fold_to_raise' posterior",
    ),
)
def test_quote_glued_shared_leaf_cannot_escape_via_decoration_strip(scoped):
    errors = _errors(scoped)
    assert SHARED_LEAF_ERROR in errors, (
        "stripping decoration must not destroy the leaf's word boundary: "
        f"{scoped!r} -> {errors}"
    )


@pytest.mark.parametrize(
    "scoped",
    (
        "opponent.rates (aggression, foldtoraise)",
        "opponent.rates (foldtoraise, aggression)",
    ),
)
def test_root_scoped_list_accepts_compact_leaf_spelling(scoped):
    """The prompt (agent_master_prompts.py:243-247) names foldtoraise as a
    legal spelling inside the root-scoped list; the prose side already
    treats the compact form as a shared-leaf synonym, so the list side must
    grade it identically."""

    errors = _errors(scoped)
    assert errors == (), (
        f"compact leaf spelling inside its root-scoped list rejected: {errors}"
    )


def test_compact_unknown_leaf_still_rejected():
    errors = _errors("opponent.rates (aggression, foldtoraisee)")
    assert (
        "proposal_mechanism_root_scoped_unknown_leaf:opponent.rates:foldtoraisee"
        in errors
    ), f"unknown compact leaf accepted: {errors}"


@pytest.mark.parametrize(
    ("primary", "root"),
    (
        ("showdown_range", "opponent.showdown_range"),
        ("terminal_response", "opponent.terminal_response"),
    ),
)
def test_shared_leaf_guidance_example_follows_the_allowed_primary(primary, root):
    """The example list must be rendered from the same allowed_primaries as
    root_clause; a hardcoded opponent.rates example beside "the only
    executable root is opponent.showdown_range" teaches a copy that trips
    foreign-target and unknown-leaf checks."""
    import agent_master_validation as amv

    guidance = amv._proposal_schema_repair_guidance(
        (SHARED_LEAF_ERROR,),
        require_snapshot_evidence=False,
        allowed_primaries=(primary,),
    )
    assert f"{root} (" in guidance, (
        "the example must be derived from the retry's own allowed primary; "
        f"got: {guidance!r}"
    )
    assert "opponent.rates" not in guidance or root == "opponent.rates", (
        f"a foreign root example leaked into the {primary} retry: {guidance!r}"
    )
