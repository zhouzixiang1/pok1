"""Tests for /api/bots/* endpoints."""

import pytest
from bot_namespace import ACTIVE_BOT_PREFIX, bot_name


class TestListBots:
    def test_default(self, client):
        resp = client.get("/api/bots")
        assert resp.status_code == 200
        data = resp.json()
        assert "active" in data
        assert "graveyard" in data
        assert "history" not in data
        assert isinstance(data["active"], list)
        assert data["graveyard"] == []

    def test_with_graveyard(self, client):
        resp = client.get("/api/bots?include_graveyard=true")
        assert resp.status_code == 200
        data = resp.json()
        assert "graveyard" in data
        for bot in data["active"]:
            assert "name" in bot
            assert "version" in bot
            assert "completed" in bot
            assert "files" in bot
            assert "lifecycle_status" in bot

    def test_with_history(self, client):
        resp = client.get("/api/bots?include_history=true&include_graveyard=true")
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

    @pytest.mark.requires_active_bot
    def test_data_stream_bot_snapshot_uses_active_namespace(self):
        from server.routes.data_stream import _get_bots

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
        assert "files" in data
        assert "total_lines" in data

    def test_404(self, client):
        resp = client.get("/api/bots/9999")
        assert resp.status_code == 404


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
            assert "main.py" in names
            # No bytecode caches leaked into the archive
            assert not any("__pycache__" in n for n in names)
            assert not any(n.endswith(".pyc") for n in names)
            # Content is readable
            assert "def " in zf.read("main.py").decode("utf-8", "replace") or \
                   "import " in zf.read("main.py").decode("utf-8", "replace")

    def test_404(self, client):
        resp = client.get("/api/bots/9999/download")
        assert resp.status_code == 404


class TestBotDownloadSymlinkDefense:
    def test_symlink_excluded_from_zip(self, client, monkeypatch, tmp_path):
        """Symlinks inside a bot dir must not leak external files into the zip."""
        import io
        import zipfile
        from server.routes import bots as bots_mod

        bot_dir = tmp_path / bot_name(9999)
        bot_dir.mkdir()
        (bot_dir / "main.py").write_text("def main():\n    pass\n")
        (bot_dir / ".completed").touch()
        # An external file that must NEVER appear in the archive
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP_SECRET_LEAK")
        # A symlink inside the bot dir pointing at the external secret
        (bot_dir / "link_to_secret.py").symlink_to(secret)

        monkeypatch.setattr(bots_mod, "BOTS_DIR", tmp_path)
        resp = client.get("/api/bots/9999/download")
        assert resp.status_code == 200

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
            assert "main.py" in names
            # Symlink itself and its target both absent
            assert "link_to_secret.py" not in names
            assert "secret.txt" not in names
            # No archive entry carries the secret content
            for n in names:
                assert b"TOP_SECRET_LEAK" not in zf.read(n)



@pytest.mark.requires_active_bot
class TestBotCode:
    def test_read_main(self, client, active_bot_version):
        resp = client.get(f"/api/bots/{active_bot_version}/code/main.py")
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
