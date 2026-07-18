"""Tests for /api/bots/* endpoints."""

import json
from pathlib import Path

import pytest
from bot_namespace import (
    ACTIVE_BOT_PREFIX,
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_EPOCH_RECEIPT,
    bot_name,
    build_policy_epoch_receipt,
    build_runtime_manifest,
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
    parents = () if version == 143 else (143,)
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

    def test_retired_graveyard_query_has_no_catalog_surface(self, client):
        resp = client.get("/api/bots?include_graveyard=true")
        assert resp.status_code == 200
        data = resp.json()
        assert "graveyard" not in data
        for bot in data["active"]:
            assert "name" in bot
            assert "version" in bot
            assert "completed" in bot
            assert "files" in bot
            assert "lifecycle_status" in bot

    def test_with_history(self, client):
        resp = client.get("/api/bots?include_history=true")
        assert resp.status_code == 200
        data = resp.json()
        assert "history" in data
        assert "counts" in data
        assert isinstance(data["history"], list)
        assert isinstance(data["counts"], dict)
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

        bots_root = tmp_path / "bots"
        (bots_root / "national_v143").mkdir(parents=True)
        (bots_root / "national_v155").mkdir()
        monkeypatch.setattr(bots_mod, "BOTS_DIR", bots_root)
        monkeypatch.setattr(
            bots_mod,
            "build_bot_summary",
            lambda path, name, *_args, **_kwargs: {
                "name": name,
                "version": int(name.removeprefix("national_v")),
                "completed": True,
            },
        )

        result = bots_mod.build_bot_listing(
            {"national_v143": {"r": 1500, "rd": 80}},
            {"national_v143": {"games": 1}},
            {},
            include_history=True,
            active_names=["national_v143"],
        )

        assert [row["name"] for row in result["active"]] == ["national_v143"]
        assert [row["name"] for row in result["history"]] == ["national_v143"]
        assert "national_v155" not in json.dumps(result)

    def test_backend_ordinals_survive_sorting_and_pool_filtering(
        self, monkeypatch, tmp_path
    ):
        from server.routes import bots as bots_mod

        bots_root = tmp_path / "bots"
        for name in ("national_v143", "national_v144"):
            (bots_root / name).mkdir(parents=True)
        monkeypatch.setattr(bots_mod, "BOTS_DIR", bots_root)
        monkeypatch.setattr(
            bots_mod,
            "build_bot_summary",
            lambda _path, name, *_args, **_kwargs: {
                "name": name,
                "version": int(name.removeprefix("national_v")),
                "completed": True,
            },
        )

        both = bots_mod.build_bot_listing(
            {}, {}, {},
            include_history=False,
            active_names=["national_v144", "national_v143"],
        )["active"]
        assert [(row["canonical_bot_name"], row["generation_ordinal"]) for row in both] == [
            ("national_v143", 1),
            ("national_v144", 2),
        ]
        only_second = bots_mod.build_bot_listing(
            {}, {}, {},
            include_history=False,
            active_names=["national_v144"],
        )["active"]
        assert only_second[0]["generation_ordinal"] == 2
        assert only_second[0]["canonical_tag"] == "national-bot-v144"

    def test_published_summary_name_version_mismatch_fails_closed(self):
        from server.routes import bots as bots_mod

        with pytest.raises(
            ValueError,
            match="published_bot_summary_canonical_name_mismatch",
        ):
            bots_mod._decorate_published({
                "name": "national_v143",
                "version": 144,
            })

    @pytest.mark.requires_active_bot
    def test_data_stream_bot_snapshot_uses_active_namespace(self, monkeypatch):
        import evolution_infra
        from server.routes.data_stream import _get_bots

        # The global isolation fixture exposes read-only bots through symlinks.
        # Formal artifact identity rejects symlink roots and the current
        # protocol quarantine rejects the historical fixture versions. This
        # route test verifies namespace projection, not either eligibility
        # authority, so both filters are replaced explicitly.
        monkeypatch.setattr(
            evolution_infra,
            "_official_parent_eligible",
            lambda _bot_dir: True,
        )
        monkeypatch.setattr(
            evolution_infra,
            "is_active_bot_protocol_eligible",
            lambda _version: True,
        )
        data = _get_bots()
        assert data["active"]
        assert all(bot["name"].startswith(ACTIVE_BOT_PREFIX) for bot in data["active"])


class TestBotDetail:
    @pytest.mark.requires_active_bot
    def test_found(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == bot_name(active_bot_version)
        assert data["version"] == active_bot_version
        assert data["canonical_version"] == active_bot_version
        assert data["canonical_bot_name"] == bot_name(active_bot_version)
        assert data["canonical_tag"] == f"national-bot-v{active_bot_version}"
        assert data["generation_ordinal"] == active_bot_version - 142
        assert "files" in data
        assert "total_lines" in data
        assert "official_certification" in data

    def test_404(self, client):
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
            lambda: ["national_v143"],
        )

        with pytest.raises(Exception) as exc_info:
            bots_mod._resolve_bot_dir(155)
        assert getattr(exc_info.value, "status_code", None) == 404

    def test_published_bot_stays_visible_before_first_rating_cycle(
        self, monkeypatch, tmp_path
    ):
        from server.routes import bots as bots_mod

        bot = _write_strict_bot(tmp_path, 143)
        (bot / ".completed").touch()
        monkeypatch.setattr(bots_mod, "BOTS_DIR", tmp_path / "bots")
        monkeypatch.setattr(
            bots_mod,
            "_strict_published_inventory",
            lambda: ["national_v143"],
        )
        monkeypatch.setattr(bots_mod, "_strict_snapshot", lambda: {})
        monkeypatch.setattr(
            bots_mod,
            "build_bot_summary",
            lambda _path, name, *_args, **_kwargs: {
                "name": name,
                "version": 143,
                "completed": True,
            },
        )

        result = bots_mod.build_bot_listing(
            {},
            {},
            {},
            include_history=False,
            active_names=["national_v143"],
            strength_evidence_available=False,
        )

        assert [row["name"] for row in result["active"]] == ["national_v143"]
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


@pytest.mark.requires_active_bot
class TestBotDownload:
    def test_zip_archive(self, client, active_bot_version):
        import io
        import zipfile

        resp = client.get(f"/api/bots/{active_bot_version}/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert f"{bot_name(active_bot_version)}.zip" in cd

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
            # Bot entry point must be present
            assert "national_bot.py" in names
            # No bytecode caches leaked into the archive
            assert not any("__pycache__" in n for n in names)
            assert not any(n.endswith(".pyc") for n in names)
            # Content is readable
            assert zf.read("national_bot.py").decode("utf-8", "replace")

    def test_404(self, client):
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
        resp = client.get("/api/bots/9999/download")
        assert resp.status_code == 404
        assert b"TOP_SECRET_LEAK" not in resp.content



@pytest.mark.requires_active_bot
class TestBotCode:
    def test_read_native_entry(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/national_bot.py")
        assert resp.status_code == 200
        assert "def " in resp.text or "import " in resp.text

    def test_invalid_filename(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/../etc/passwd")
        assert resp.status_code == 404

    def test_non_py_file(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/main.txt")
        assert resp.status_code == 400

    def test_404(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/nonexistent.py")
        assert resp.status_code == 404

    def test_backslash_blocked(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/..\\etc")
        assert resp.status_code == 400
