"""Per-plan deck-seed nonce regressions (2026-10-11).

The production precommit plan path derives deck_seed_base from a fresh
128-bit nonce at the plan-freeze moment; the derivation must be
unpredictable across plans, deterministically re-provable by
validate_precommit_plan, tamper-evident, and byte-stable for the explicit
diagnostic base path.
"""

from pathlib import Path

import precommit_eval_contract as contract
import pool_ratchet_gate
from bot_namespace import bot_name, parse_bot_version


def _published(path: Path, *, artifact_hash: str = "a" * 64) -> dict:
    return {
        "published": True,
        "artifact_hash": artifact_hash,
        "tag": f"national-cloud-bot-v{parse_bot_version(str(path.name))}",
        "tag_type": "tag",
        "tag_object": "b" * 40,
        "commit_oid": "c" * 40,
        "completion_tree_oid": "d" * 40,
        "main_tree_oid": "d" * 40,
        "issues": [],
    }


def _make_nonce_plan(tmp_path, monkeypatch, *, matches=2):
    opponents = []
    identities = {}
    for version in (8, 5):
        path = tmp_path / bot_name(version)
        path.mkdir(exist_ok=True)
        (path / "national_bot.py").write_text("# native\n", encoding="utf-8")
        opponents.append(
            {"name": path.name, "reason": "parent" if version == 8 else "top"}
        )
        identities[str(path)] = _published(
            path, artifact_hash=str(version) * 64
        )
    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda path: dict(identities[str(Path(path))]),
    )
    plan = contract.create_precommit_plan(
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
        opponents=opponents,
        hands_per_match=70,
        matches_per_opponent=matches,
        path_resolver=lambda item: tmp_path / item["name"],
        require_published_opponents=True,
    )
    return plan


def _validate(plan):
    return contract.validate_precommit_plan(
        plan,
        candidate_version=9,
        source_version=8,
        profile_id="national_native",
        execution_mode="native_tcp",
        evaluation_protocol="national",
    )


def test_two_plans_derive_independent_unpredictable_bases(
    tmp_path, monkeypatch
):
    first = _make_nonce_plan(tmp_path, monkeypatch)
    second = _make_nonce_plan(tmp_path, monkeypatch)

    settings_first = first["settings"]
    settings_second = second["settings"]
    assert settings_first["seed_derivation"] == "nonce_sha256_v1"
    assert settings_second["seed_derivation"] == "nonce_sha256_v1"
    # 128-bit nonces differ and neither base is the historical fixed
    # DEFAULT_DECK_SEED_BASE (91_000); the version number alone cannot
    # predict either.
    assert settings_first["deck_seed_nonce"] != settings_second["deck_seed_nonce"]
    assert settings_first["deck_seed_base"] != settings_second["deck_seed_base"]
    assert settings_first["deck_seed_base"] != contract.DEFAULT_DECK_SEED_BASE
    assert settings_second["deck_seed_base"] != contract.DEFAULT_DECK_SEED_BASE
    assert _validate(first) == []
    assert _validate(second) == []


def test_sample_plan_keeps_index_expansion_and_decoupled_bot_seed(
    tmp_path, monkeypatch
):
    plan = _make_nonce_plan(tmp_path, monkeypatch, matches=3)
    base = plan["settings"]["deck_seed_base"]
    nonce = plan["settings"]["deck_seed_nonce"]
    bot_root = pool_ratchet_gate.derive_bot_seed_root(nonce)
    assert plan["settings"]["deck_seed_base"] == pool_ratchet_gate.derive_deck_seed_base(
        nonce
    )
    # Red-team fix: the bot-side root is domain-separated — observing the
    # bot's own seed (env or RNG stream) must not derive the deck schedule.
    assert bot_root != base
    assert bot_root != base + 1_000_000_000
    for row in plan["sample_plan"]:
        expected_deck = (
            base + row["opponent_index"] * 100_000 + (row["repeat"] - 1) * 1_000
        )
        expected_bot = (
            bot_root
            + row["opponent_index"] * 100_000
            + (row["repeat"] - 1) * 1_000
        )
        assert row["deck_seed_base"] == expected_deck
        assert row["bot_seed_base"] == expected_bot
        # The historical linear relation must NOT hold on the nonce path.
        assert row["bot_seed_base"] != row["deck_seed_base"] + 1_000_000_000


def test_bot_seed_tamper_fails_validation(tmp_path, monkeypatch):
    plan = _make_nonce_plan(tmp_path, monkeypatch, matches=2)
    # Tamper the sample row: the rebuild re-proves the decoupled relation.
    plan["sample_plan"][0]["bot_seed_base"] = (
        plan["sample_plan"][0]["deck_seed_base"] + 1_000_000_000
    )
    plan["plan_digest"] = contract.canonical_digest(
        {k: v for k, v in plan.items() if k != "plan_digest"}
    )
    issues = _validate(plan)
    assert "precommit_sample_plan_mismatch" in issues

    # Tamper the settings-side batch plan row instead (digests recomputed
    # around the forged bytes): the batch rebuild must still reject a
    # linearly-related bot seed on the nonce path.
    plan = _make_nonce_plan(tmp_path, monkeypatch, matches=2)
    batch = plan["settings"]["native_precommit_batch_plan"]
    batch["ordered_samples"][0]["bot_seed_base"] = (
        batch["ordered_samples"][0]["deck_seed_base"] + 1_000_000_000
    )
    unsigned_batch = {
        k: v for k, v in batch.items() if k != "batch_plan_digest"
    }
    batch["batch_plan_digest"] = contract.canonical_digest(unsigned_batch)
    plan["settings"]["native_precommit_batch_plan_digest"] = (
        batch["batch_plan_digest"]
    )
    unsigned_plan = {
        k: v for k, v in plan.items() if k != "plan_digest"
    }
    plan["plan_digest"] = contract.canonical_digest(unsigned_plan)
    issues = _validate(plan)
    assert "precommit_native_batch_plan_mismatch" in issues


def test_tampered_nonce_base_or_settings_fails_closed(tmp_path, monkeypatch):
    plan = _make_nonce_plan(tmp_path, monkeypatch)

    # Tampering the nonce breaks the derivation re-proof even when the plan
    # digest is recomputed around the tampered bytes.
    forged_nonce = dict(plan)
    forged_nonce["settings"] = {
        **plan["settings"],
        "deck_seed_nonce": "f" * 32,
    }
    forged_nonce["plan_digest"] = contract.canonical_digest(
        {k: v for k, v in forged_nonce.items() if k != "plan_digest"}
    )
    issues = _validate(forged_nonce)
    assert "precommit_seed_derivation_mismatch" in issues

    # Tampering only the base (digest left stale) fails the digest AND the
    # derivation re-proof.
    forged_base = dict(plan)
    forged_base["settings"] = {**plan["settings"], "deck_seed_base": 91_000}
    issues = _validate(forged_base)
    assert "precommit_plan_digest_mismatch" in issues
    assert "precommit_seed_derivation_mismatch" in issues

    # An unknown derivation label is invalid.
    forged_label = dict(plan)
    forged_label["settings"] = {
        **plan["settings"],
        "seed_derivation": "mystery_v9",
    }
    issues = _validate(forged_label)
    assert "precommit_seed_derivation_invalid" in issues

    # An explicit-base plan must not claim a nonce.
    mixed = dict(plan)
    mixed["settings"] = {
        **plan["settings"],
        "seed_derivation": "explicit_base",
    }
    issues = _validate(mixed)
    assert "precommit_seed_derivation_mismatch" in issues


def test_explicit_base_path_is_deterministic_and_replayable(
    tmp_path, monkeypatch
):
    opponents = []
    identities = {}
    for version in (8,):
        path = tmp_path / bot_name(version)
        path.mkdir()
        (path / "national_bot.py").write_text("# native\n", encoding="utf-8")
        opponents.append({"name": path.name, "reason": "parent"})
        identities[str(path)] = _published(path)
    monkeypatch.setattr(
        contract,
        "published_bot_identity",
        lambda path: dict(identities[str(Path(path))]),
    )

    def build():
        return contract.create_precommit_plan(
            candidate_version=9,
            source_version=8,
            profile_id="national_native",
            execution_mode="native_tcp",
            evaluation_protocol="national",
            opponents=list(opponents),
            hands_per_match=70,
            matches_per_opponent=2,
            path_resolver=lambda item: tmp_path / item["name"],
            require_published_opponents=True,
            deck_seed_base=91_000,
        )

    first = build()
    second = build()
    assert first == second
    assert first["settings"]["seed_derivation"] == "explicit_base"
    assert first["settings"]["deck_seed_nonce"] is None
    assert first["sample_plan"][0]["deck_seed_base"] == 91_000
    assert _validate(first) == []


def test_derive_deck_seed_base_is_deterministic_and_validated():
    nonce = "0123456789abcdef" * 2
    assert pool_ratchet_gate.derive_deck_seed_base(nonce) == (
        pool_ratchet_gate.derive_deck_seed_base(nonce)
    )
    import hashlib

    assert pool_ratchet_gate.derive_deck_seed_base(nonce) == int.from_bytes(
        hashlib.sha256(nonce.encode("utf-8")).digest()[:6], "big"
    )
    for bad in ("", "zz", "abc", "0" * 31, "0" * 33):
        try:
            pool_ratchet_gate.derive_deck_seed_base(bad)
        except ValueError:
            continue
        raise AssertionError(f"nonce {bad!r} must be rejected")
