from __future__ import annotations

from pathlib import Path


def _published_identity(path: Path) -> dict[str, object]:
    version = int(path.name.removeprefix("national_v"))
    return {
        "published": True,
        "label": path.name,
        "version": version,
        "tag": f"national-bot-v{version}",
    }


def _certificate(_path: Path) -> dict[str, object]:
    return {"eligible": True, "certificate_digest": "a" * 64}


def test_system_runtime_identity_accepts_only_exact_current_bytes(tmp_path):
    from national_runtime_authority import current_system_native_runtime_errors
    from system_strict_bootstrap import materialize_fresh_candidate

    bot = tmp_path / "national_v143"
    materialize_fresh_candidate(bot, final_policy=True)
    assert current_system_native_runtime_errors(bot) == []

    entry = bot / "national_bot.py"
    entry.write_bytes(entry.read_bytes() + b"\n# candidate edit\n")
    errors = current_system_native_runtime_errors(bot)
    assert len(errors) == 1
    assert errors[0].startswith("system_owned_native_runtime_identity_mismatch:")


def test_strict_discovery_has_no_pre_policy_or_quarantine_path(tmp_path):
    from national_runtime_authority import strict_published_bot_names
    from system_strict_bootstrap import materialize_fresh_candidate

    bots = tmp_path / "bots"
    bots.mkdir()
    materialize_fresh_candidate(bots / "national_v143", final_policy=True)
    (bots / "national_v142").mkdir()

    assert strict_published_bot_names(
        bots_dir=bots,
        publication_resolver=_published_identity,
        certificate_resolver=_certificate,
    ) == ("national_v143",)


def test_runtime_authority_has_no_archived_identity_registry():
    import national_runtime_authority as authority

    assert not hasattr(authority, "quarantined_native_entry_sources")
    assert not hasattr(authority, "select_protocol_bootstrap_source")
    assert not hasattr(authority, "MIGRATION_SEED_VERSION")
