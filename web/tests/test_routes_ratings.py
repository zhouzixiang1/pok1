"""Tests for /api/ratings, /api/history, /api/experience, /api/daemon/status, /api/h2h, /api/bot-stats."""


class TestGetRatings:
    def test_returns_list(self, client):
        resp = client.get("/api/ratings")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            row = data[0]
            assert "name" in row
            assert "rating" in row
            assert "rd" in row
            assert "rank" in row
            assert "h2h_avg_wr" in row

    def test_detail_found(self, client, active_bot_version):
        if active_bot_version is None:
            return
        resp = client.get(f"/api/ratings/claude_v{active_bot_version}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == f"claude_v{active_bot_version}"
        assert "rating" in data

    def test_detail_404(self, client):
        resp = client.get("/api/ratings/nonexistent_bot")
        assert resp.status_code == 404


class TestHistory:
    def test_default(self, client):
        resp = client.get("/api/history")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            assert "period" in data[0]
            assert "ratings" in data[0]
            assert "win_rates" in data[0]

    def test_filtered(self, client, active_bot_version):
        if active_bot_version is None:
            return
        resp = client.get(f"/api/history?bots=claude_v{active_bot_version}")
        assert resp.status_code == 200

    def test_summary(self, client):
        resp = client.get("/api/history/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)


class TestExperience:
    def test_get(self, client):
        resp = client.get("/api/experience")
        assert resp.status_code == 200
        assert isinstance(resp.text, str)

    def test_update(self, client, temp_experience, monkeypatch):
        from server.routes import ratings
        monkeypatch.setattr(ratings, "EXPERIENCE_FILE", temp_experience)
        resp = client.put("/api/experience", json={"content": "## Updated\n- New lesson\n"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["saved"] is True
        assert temp_experience.read_text() == "## Updated\n- New lesson\n"

    def test_append(self, client, temp_experience, monkeypatch):
        from server.routes import ratings
        monkeypatch.setattr(ratings, "EXPERIENCE_FILE", temp_experience)
        resp = client.post("/api/experience/append", json={"lesson": "Test lesson"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["appended"] is True
        content = temp_experience.read_text()
        assert "Test lesson" in content

    def test_append_empty(self, client, temp_experience, monkeypatch):
        from server.routes import ratings
        monkeypatch.setattr(ratings, "EXPERIENCE_FILE", temp_experience)
        resp = client.post("/api/experience/append", json={"lesson": ""})
        assert resp.status_code == 400


class TestDaemonStatus:
    def test_returns_status(self, client):
        resp = client.get("/api/daemon/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "last_update_age_seconds" in data
        assert "daemon_enabled" in data


class TestH2H:
    def test_all(self, client):
        resp = client.get("/api/h2h")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_filtered(self, client, active_bot_version):
        if active_bot_version is None:
            return
        bot_name = f"claude_v{active_bot_version}"
        resp = client.get(f"/api/h2h?bot_name={bot_name}")
        assert resp.status_code == 200
        data = resp.json()
        for key in data:
            assert bot_name in key


class TestBotStats:
    def test_returns_dict(self, client):
        resp = client.get("/api/bot-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
