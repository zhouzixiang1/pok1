"""Tests for /api/bots/* endpoints."""

import json
from pathlib import Path

import pytest
from bot_namespace import (
    ACTIVE_BOT_PREFIX,
    FIRST_STRICT_POLICY_VERSION,
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_EPOCH_RECEIPT,
    bot_name,
    bot_tag,
    build_policy_epoch_receipt,
    build_runtime_manifest,
    parse_bot_version,
)


def _write_strict_bot(root: Path, version: int) -> Path:
    bot = root / "bots" / bot_name(version)
    bot.mkdir(parents=True)
    (bot / "national_bot.py").write_text("# native TCP entry\n", encoding="utf-8")
    (bot / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    return ()\n",
        encoding="utf-8",
    )
    (bot / "precompute.py").write_text("TABLE = ()\n", encoding="utf-8")
    manifest = build_runtime_manifest(bot)
    (bot / NATIONAL_RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    first_strict = FIRST_STRICT_POLICY_VERSION
    parents = () if version == first_strict else (first_strict,)
    receipt = build_policy_epoch_receipt(bot, version, parent_versions=parents)
    (bot / POLICY_EPOCH_RECEIPT).write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    return bot


class TestListBots:
    def test_default(self, client):
        resp = client.get("/api/bots")
        assert resp.status_code == 200
        data = resp.json()
        assert "active" in data
        assert "graveyard" not in data
        assert "history" not in data
        assert isinstance(data["active"], list)

    def test_retired_graveyard_query_has_no_catalog_surface(
        self, client, synthetic_published_bot_authority
    ):
        resp = client.get("/api/bots?include_graveyard=true")
        assert resp.status_code == 200
        data = resp.json()
        assert "graveyard" not in data
        assert len(data["active"]) == 2
        for bot in data["active"]:
            assert "name" in bot
            assert "version" in bot
            assert "completed" in bot
            assert "files" in bot
            assert "lifecycle_status" in bot

    def test_with_history(self, client, synthetic_published_bot_authority):
        resp = client.get("/api/bots?include_history=true")
        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data
        assert "counts" in data
        assert isinstance(data["history"], list)
        assert isinstance(data["counts"], dict)
        assert len(data["history"]) == 2
        for bot in data["history"][:5]:
            assert "lifecycle_status" in bot
            assert "status_label" in bot
            assert "status_reasons" in bot
            assert "protocol_errors" in bot
            assert "official_certification" in bot
            assert "status" in bot["official_certification"]

    def test_listing_never_includes_unpublished_directory(
        self, monkeypatch, tmp_path
    ):
        from server.routes import bots as bots_mod

        published = bot_name(143)
        unpublished = bot_name(155)
        bots_root = tmp_path / "bots"
        (bots_root / published).mkdir(parents=True)
        (bots_root / unpublished).mkdir()
        monkeypatch.setattr(bots_mod, "BOTS_DIR", bots_root)
        monkeypatch.setattr(
            bots_mod,
            "build_bot_summary",
            lambda path, name, *_args, **_kwargs: {
                "name": name,
                "version": parse_bot_version(name),
                "completed": True,
            },
        )

        result = bots_mod.build_bot_listing(
            {published: {"r": 1500, "rd": 80}},
            {published: {"games": 1}},
            {},
            include_history=True,
            active_names=[published],
            generation_identities={
                published: {
                    "generation_ordinal": 1,
                    "canonical_version": 143,
                    "canonical_bot_name": published,
                    "canonical_tag": bot_tag(143),
                },
            },
        )

        assert [row["name"] for row in result["active"]] == [published]
        assert [row["name"] for row in result["history"]] == [published]
        assert unpublished not in json.dumps(result)

    def test_backend_ordinals_survive_sorting_and_pool_filtering(
        self, monkeypatch, tmp_path
    ):
        from server.routes import bots as bots_mod

        name_a = bot_name(143)
        name_b = bot_name(144)
        bots_root = tmp_path / "bots"
        for name in (name_a, name_b):
            (bots_root / name).mkdir(parents=True)
        monkeypatch.setattr(bots_mod, "BOTS_DIR", bots_root)
        monkeypatch.setattr(
            bots_mod,
            "build_bot_summary",
            lambda _path, name, *_args, **_kwargs: {
                "name": name,
                "version": parse_bot_version(name),
                "completed": True,
            },
        )

        both = bots_mod.build_bot_listing(
            {}, {}, {},
            include_history=False,
            active_names=[name_b, name_a],
            generation_identities={
                name_a: {
                    "generation_ordinal": 1,
                    "canonical_version": 143,
                    "canonical_bot_name": name_a,
                    "canonical_tag": bot_tag(143),
                },
                name_b: {
                    "generation_ordinal": 2,
                    "canonical_version": 144,
                    "canonical_bot_name": name_b,
                    "canonical_tag": bot_tag(144),
                },
            },
        )["active"]
        assert [(row["canonical_bot_name"], row["generation_ordinal"]) for row in both] == [
            (name_a, 1),
            (name_b, 2),
        ]
        only_second = bots_mod.build_bot_listing(
            {}, {}, {},
            include_history=False,
            active_names=[name_b],
            generation_identities={
                name_b: {
                    "generation_ordinal": 2,
                    "canonical_version": 144,
                    "canonical_bot_name": name_b,
                    "canonical_tag": bot_tag(144),
                },
            },
        )["active"]
        assert only_second[0]["generation_ordinal"] == 2
        assert only_second[0]["canonical_tag"] == bot_tag(144)

    def test_published_summary_name_version_mismatch_fails_closed(self):
        from server.routes import bots as bots_mod

        with pytest.raises(
            ValueError,
            match="published_bot_summary_canonical_name_mismatch",
        ):
            bots_mod._decorate_published(
                {
                    "name": bot_name(143),
                    "version": 144,
                },
                {
                    "generation_ordinal": 2,
                    "canonical_version": 144,
                    "canonical_bot_name": bot_name(144),
                    "canonical_tag": bot_tag(144),
                },
            )

    def test_swapped_backend_ordinals_withhold_entire_publication_projection(
        self,
        monkeypatch,
    ):
        import epoch_authority
        from server.routes import bots as bots_mod

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: {
                "initialized": True,
                "active_bots": [bot_name(143), bot_name(147)],
                "strict_published_bot_identities": [
                    {
                        "generation_ordinal": 2,
                        "canonical_version": 143,
                        "canonical_bot_name": bot_name(143),
                        "canonical_tag": bot_tag(143),
                    },
                    {
                        "generation_ordinal": 1,
                        "canonical_version": 147,
                        "canonical_bot_name": bot_name(147),
                        "canonical_tag": bot_tag(147),
                    },
                ],
            },
        )

        assert bots_mod._strict_published_authority() == ([], {})

    def test_abandoned_version_gap_keeps_contiguous_published_ordinals(
        self,
        monkeypatch,
    ):
        import epoch_authority
        from server.routes import bots as bots_mod
        from conftest import STRICT_TARGET_V

        first_v = STRICT_TARGET_V
        second_v = STRICT_TARGET_V + 4
        identities = [
            {
                "generation_ordinal": 1,
                "canonical_version": first_v,
                "canonical_bot_name": bot_name(first_v),
                "canonical_tag": bot_tag(first_v),
            },
            {
                "generation_ordinal": 2,
                "canonical_version": second_v,
                "canonical_bot_name": bot_name(second_v),
                "canonical_tag": bot_tag(second_v),
            },
        ]
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: {
                "initialized": True,
                "active_bots": [bot_name(second_v)],
                "strict_published_bot_identities": identities,
            },
        )

        names, by_name = bots_mod._strict_published_authority()
        assert names == [bot_name(second_v)]
        assert by_name[bot_name(first_v)]["generation_ordinal"] == 1
        assert by_name[bot_name(second_v)]["generation_ordinal"] == 2

    def test_data_stream_bot_snapshot_uses_active_namespace(
        self, synthetic_published_bot_authority
    ):
        from server.routes.data_stream import _get_bots

        data = _get_bots()
        assert [row["name"] for row in data["active"]] == list(
            synthetic_published_bot_authority["names"]
        )
        assert all(bot["name"].startswith(ACTIVE_BOT_PREFIX) for bot in data["active"])

    def test_list_bots_offloads_blocking_work(self, monkeypatch, client):
        """list_bots must run its body via run_blocking_isolated.

        The pool read transitively performs blocking git/file operations
        (including ``git ls-remote origin`` under POK_REQUIRE_EVOLUTION_PUSH=1).
        Running that inline in the async handler freezes the shared uvicorn
        event loop and starves every other endpoint (notably health). This
        test asserts the offload boundary is in place by spying on
        ``run_blocking_isolated``.
        """
        from server.routes import bots as bots_mod

        calls = {"count": 0, "prefix": None}

        real_offload = bots_mod.run_blocking_isolated

        async def spy_offload(func, *args, thread_name_prefix=None, **kwargs):
            calls["count"] += 1
            calls["prefix"] = thread_name_prefix
            return await real_offload(
                func, *args, thread_name_prefix=thread_name_prefix, **kwargs
            )

        monkeypatch.setattr(bots_mod, "run_blocking_isolated", spy_offload)

        resp = client.get("/api/bots")
        assert resp.status_code == 200
        assert calls["count"] == 1, "list_bots must offload exactly once"
        assert calls["prefix"] == "list-bots"

    def test_list_bots_blocking_runs_without_event_loop(self):
        """The extracted synchronous helper is pure and needs no running loop.

        This guards against a regression where blocking logic leaks back into
        the async handler body.
        """
        from server.routes.bots import _list_bots_blocking

        # With an uninitialized epoch, the helper must return a fail-closed
        # listing rather than raise — proving it is safe to run on a worker.
        result = _list_bots_blocking(include_history=False)
        assert isinstance(result, dict)
        assert "active" in result


class TestRemotePublicationCacheTtl:
    def test_ttl_constant_reads_env_override(self, monkeypatch):
        """The remote-publication proof cache TTL must be env-overridable.

        Default 60s keeps read-only observer requests off the network during a
        poll burst; the previous hardcoded 5s caused a slow origin (GitHub
        ls-remote ~30-60s on a constrained link) to stall the API.
        """
        import importlib

        monkeypatch.setenv("POK_REMOTE_PUBLICATION_CACHE_TTL", "42")
        import evolution_infra

        importlib.reload(evolution_infra)
        try:
            assert evolution_infra._REMOTE_PUBLICATION_CACHE_TTL_SEC == 42.0
        finally:
            monkeypatch.delenv("POK_REMOTE_PUBLICATION_CACHE_TTL", raising=False)
            importlib.reload(evolution_infra)

    def test_ttl_default_is_60_seconds(self):
        """Without an env override the TTL defaults to 60s (not the old 5s)."""
        import evolution_infra

        # Only assert when the env is genuinely unset, so a developer's local
        # POK_REMOTE_PUBLICATION_CACHE_TTL doesn't make this test flaky.
        import os

        if "POK_REMOTE_PUBLICATION_CACHE_TTL" not in os.environ:
            assert evolution_infra._REMOTE_PUBLICATION_CACHE_TTL_SEC == 60.0


class TestBotDetail:
    def test_found(self, client, synthetic_published_bot_authority):
        version = synthetic_published_bot_authority["primary_version"]
        resp = client.get(f"/api/bots/{version}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == bot_name(version)
        assert data["version"] == version
        assert data["canonical_version"] == version
        assert data["canonical_bot_name"] == bot_name(version)
        assert data["canonical_tag"] == bot_tag(version)
        expected_identity = next(
            identity
            for identity in synthetic_published_bot_authority["identities"]
            if identity["canonical_version"] == version
        )
        assert data["generation_ordinal"] == expected_identity["generation_ordinal"]
        assert "files" in data
        assert "total_lines" in data
        assert "official_certification" in data

    def test_404(self, client, synthetic_published_bot_authority):
        resp = client.get("/api/bots/9999")
        assert resp.status_code == 404

    def test_unpublished_strict_directory_is_not_resolvable(
        self, monkeypatch, tmp_path
    ):
        from server.routes import bots as bots_mod

        (tmp_path / "bots" / "national_v155").mkdir(parents=True)
        monkeypatch.setattr(bots_mod, "BOTS_DIR", tmp_path / "bots")
        monkeypatch.setattr(
            bots_mod,
            "_strict_published_inventory",
            lambda: [bot_name(143)],
        )

        with pytest.raises(Exception) as exc_info:
            bots_mod._resolve_bot_dir(155)
        assert getattr(exc_info.value, "status_code", None) == 404

    def test_published_bot_stays_visible_before_first_rating_cycle(
        self, monkeypatch, tmp_path
    ):
        from server.routes import bots as bots_mod
        from conftest import STRICT_TARGET_V

        version = STRICT_TARGET_V
        name = bot_name(version)
        bot = _write_strict_bot(tmp_path, version)
        (bot / ".completed").touch()
        monkeypatch.setattr(bots_mod, "BOTS_DIR", tmp_path / "bots")
        monkeypatch.setattr(
            bots_mod,
            "_strict_published_inventory",
            lambda: [name],
        )
        monkeypatch.setattr(bots_mod, "_strict_snapshot", lambda: {})
        monkeypatch.setattr(
            bots_mod,
            "build_bot_summary",
            lambda _path, name, *_args, **_kwargs: {
                "name": name,
                "version": version,
                "completed": True,
            },
        )

        result = bots_mod.build_bot_listing(
            {},
            {},
            {},
            include_history=False,
            active_names=[name],
            generation_identities={
                name: {
                    "generation_ordinal": 1,
                    "canonical_version": version,
                    "canonical_bot_name": name,
                    "canonical_tag": bot_tag(version),
                },
            },
            strength_evidence_available=False,
        )

        assert [row["name"] for row in result["active"]] == [name]
        assert result["active"][0]["strength_evidence_available"] is False
        assert result["active"][0]["strength_evidence_status"] == "awaiting_first_rating_cycle"


class TestCertificationRoutes:
    def test_jobs_are_empty_when_epoch_has_no_attached_job(self, client, monkeypatch):
        from server.routes import certification as cert_mod

        monkeypatch.setattr(cert_mod, "strict_epoch_projection", lambda: {
            "state": "reset_required",
            "initialized": False,
            "reset_receipt_valid": False,
            "active_bots": [],
            "active_generation": None,
        })

        jobs = client.get("/api/certification/jobs")
        assert jobs.status_code == 200
        assert jobs.json()["pending"] == 0
        assert jobs.json()["jobs"] == []
        assert jobs.json()["evaluation_epoch"] == "national_tcp_policy_v1"

    def test_http_enqueue_is_authenticated_and_retired(self, client, monkeypatch):
        monkeypatch.setenv("POK_CONTROL_TOKEN", "route-test-token")

        forbidden = client.post(
            "/api/certification/9998/enqueue?mode=smoke",
            headers={"Origin": "https://attacker.example"},
        )
        assert forbidden.status_code == 403

        response = client.post(
            "/api/certification/9998/enqueue?mode=compliance",
            headers={"X-Control-Token": "route-test-token"},
        )

        assert response.status_code == 410
        assert response.json()["code"] == "certification_http_enqueue_retired"
        assert response.json()["formal_mode"] == "full"
        assert response.json()["normal_entrypoint"] == "commit_bot"


class TestBotDownload:
    def test_zip_archive(self, client, synthetic_published_bot_authority):
        import io
        import zipfile

        version = synthetic_published_bot_authority["primary_version"]
        resp = client.get(f"/api/bots/{version}/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert f"{bot_name(version)}.zip" in cd

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
            # Bot entry point must be present
            assert "national_bot.py" in names
            # No bytecode caches leaked into the archive
            assert not any("__pycache__" in n for n in names)
            assert not any(n.endswith(".pyc") for n in names)
            # Content is readable
            assert zf.read("national_bot.py").decode("utf-8", "replace")

    def test_404(self, client, synthetic_published_bot_authority):
        resp = client.get("/api/bots/9999/download")
        assert resp.status_code == 404


class TestBotDownloadSymlinkDefense:
    def test_symlink_invalidates_strict_artifact_before_zip(
        self, client, monkeypatch, tmp_path
    ):
        """A symlink makes the exact-five artifact ineligible for download."""
        from server.routes import bots as bots_mod

        bot_dir = _write_strict_bot(tmp_path, 9999)
        (bot_dir / ".completed").touch()
        # An external file that must NEVER appear in the archive
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP_SECRET_LEAK")
        # A symlink inside the bot dir pointing at the external secret
        (bot_dir / "link_to_secret.py").symlink_to(secret)

        monkeypatch.setattr(bots_mod, "BOTS_DIR", tmp_path / "bots")
        monkeypatch.setattr(
            bots_mod,
            "_strict_published_inventory",
            lambda: ["national_v9999"],
        )
        resp = client.get("/api/bots/9999/download")
        assert resp.status_code == 404
        assert b"TOP_SECRET_LEAK" not in resp.content



class TestBotCode:
    def test_read_native_entry(self, client, synthetic_published_bot_authority):
        version = synthetic_published_bot_authority["primary_version"]
        resp = client.get(f"/api/bots/{version}/code/national_bot.py")
        assert resp.status_code == 200
        assert "def " in resp.text or "import " in resp.text

    def test_invalid_filename(self, client, synthetic_published_bot_authority):
        version = synthetic_published_bot_authority["primary_version"]
        resp = client.get(f"/api/bots/{version}/code/../etc/passwd")
        assert resp.status_code == 404

    def test_non_py_file(self, client, synthetic_published_bot_authority):
        version = synthetic_published_bot_authority["primary_version"]
        resp = client.get(f"/api/bots/{version}/code/main.txt")
        assert resp.status_code == 400

    def test_404(self, client, synthetic_published_bot_authority):
        version = synthetic_published_bot_authority["primary_version"]
        resp = client.get(f"/api/bots/{version}/code/nonexistent.py")
        assert resp.status_code == 404

    def test_backslash_blocked(self, client, synthetic_published_bot_authority):
        version = synthetic_published_bot_authority["primary_version"]
        resp = client.get(f"/api/bots/{version}/code/..\\etc")
        assert resp.status_code == 400
