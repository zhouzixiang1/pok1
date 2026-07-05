"""Regression tests for production SPA/static routing."""

from fastapi import FastAPI
from starlette.testclient import TestClient

from server.app import _install_static_spa_routes


def _client_with_static(tmp_path):
    static_dir = tmp_path / "static"
    assets_dir = static_dir / "assets"
    assets_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text("<html><body>spa</body></html>")
    (static_dir / "favicon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (assets_dir / "app.js").write_text("console.log('ok')")

    app = FastAPI()
    _install_static_spa_routes(app, static_dir)
    return TestClient(app)


def test_unknown_api_path_returns_404(tmp_path):
    client = _client_with_static(tmp_path)
    resp = client.get("/api/not-real")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")


def test_favicon_served_as_image(tmp_path):
    client = _client_with_static(tmp_path)
    resp = client.get("/favicon.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content.startswith(b"\x89PNG")


def test_frontend_route_returns_spa(tmp_path):
    client = _client_with_static(tmp_path)
    resp = client.get("/logs")
    assert resp.status_code == 200
    assert "spa" in resp.text


def test_unknown_static_file_returns_404(tmp_path):
    client = _client_with_static(tmp_path)
    resp = client.get("/missing.js")
    assert resp.status_code == 404
