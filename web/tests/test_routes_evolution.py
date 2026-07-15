"""Tests for /api/evolution/* endpoints."""


class TestEvolutionState:
    def test_returns_state(self, client):
        resp = client.get("/api/evolution/state")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_uninitialized_epoch_never_exposes_stale_webui_state(self, client, monkeypatch):
        import epoch_authority
        from server.app import web_ui

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "reset_required",
                "initialized": False,
                "reset_receipt_valid": False,
                "reset_receipt_digest": None,
                "current_v": 142,
                "next_v": 143,
                "active_bots": [],
                "active_generation": None,
            },
        )
        monkeypatch.setattr(web_ui, "grand_cost_total", 999.0)
        monkeypatch.setattr(web_ui, "gen_cost_total", 88.0)
        monkeypatch.setitem(web_ui._state, "metrics", {"success_rate": 1.0})
        monkeypatch.setitem(web_ui._state, "ratings", [{"name": "national_v155"}])
        monkeypatch.setitem(web_ui._state, "active_bots", ["national_v155"])
        monkeypatch.setitem(web_ui._state, "status", "old generation running")
        monkeypatch.setitem(web_ui._state, "is_working", True)

        data = client.get("/api/evolution/state").json()

        assert data["epoch_state"] == "reset_required"
        assert data["epoch_initialized"] is False
        assert data["current_v"] == 142
        assert data["next_v"] == 143
        assert data["status"] == "Stopped: reset_required"
        assert data["is_working"] is False
        assert data["metrics"] == {}
        assert data["ratings"] == []
        assert data["active_bots"] == []
        assert data["pipeline_stage"] is None
        assert data["pipeline_checkpoint_revision"] is None
        assert data["grand_cost_total"] == 0.0
        assert data["gen_cost_total"] == 0.0

    def test_initialized_state_carries_reset_identity(self, client, monkeypatch):
        import epoch_authority

        receipt_digest = "a" * 64
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "fresh_bootstrap_ready",
                "initialized": True,
                "reset_receipt_valid": True,
                "reset_receipt_digest": receipt_digest,
                "version_authority_high_water": 142,
                "current_v": 142,
                "next_v": 143,
                "active_bots": [],
                "active_generation": None,
            },
        )

        data = client.get("/api/evolution/state").json()

        assert data["evaluation_epoch"] == "national_tcp_policy_v1"
        assert data["epoch_state"] == "fresh_bootstrap_ready"
        assert data["epoch_initialized"] is True
        assert data["epoch_reset_receipt_digest"] == receipt_digest
        assert len(data["stream_authority_digest"]) == 64
        assert data["current_v"] == 142
        assert data["next_v"] == 143
        assert data["pipeline_checkpoint_revision"] is None

    def test_initialized_state_carries_validated_checkpoint_revision(
        self,
        client,
        monkeypatch,
    ):
        import epoch_authority
        from server.routes import _helpers

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "fresh_bootstrap_ready",
                "initialized": True,
                "reset_receipt_valid": True,
                "reset_receipt_digest": "a" * 64,
                "version_authority_high_water": 142,
                "current_v": 142,
                "next_v": 143,
                "active_bots": [],
                "active_generation": {
                    "next_v": 143,
                    "source_v": 142,
                    "stage": "reviewed",
                    "run_id": "143#1",
                    "workflow_run_id": "workflow-v1",
                    "checkpoint_revision": 7,
                },
            },
        )
        monkeypatch.setattr(
            _helpers,
            "load_strict_pipeline_checkpoint",
            lambda *_args, **_kwargs: {
                "stage": "reviewed",
                "checkpoint_revision": 7,
            },
        )

        data = client.get("/api/evolution/state").json()

        assert data["pipeline_stage"] == "reviewed"
        assert data["pipeline_checkpoint_revision"] == 7


class TestEvolutionStream:
    def test_route_registered(self, client):
        """SSE endpoint is registered at /api/evolution/stream."""
        routes = {r.path for r in client.app.routes}
        assert "/api/evolution/stream" in routes

    def test_stream_projection_rejects_torn_epoch_handoff_sample(
        self,
        monkeypatch,
    ):
        import server.routes.evolution as route

        epochs = iter((
            {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "fresh_bootstrap_ready",
                "initialized": True,
                "reset_receipt_valid": True,
                "reset_receipt_digest": "a" * 64,
                "version_authority_high_water": 142,
                "active_bots": [],
            },
            {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "strict_published",
                "initialized": True,
                "reset_receipt_valid": True,
                "reset_receipt_digest": "a" * 64,
                "version_authority_high_water": 143,
                "active_bots": ["national_v143"],
            },
        ))
        monkeypatch.setattr(route, "_epoch_projection", lambda: next(epochs))
        monkeypatch.setattr(
            route,
            "_handoff_projection",
            lambda epoch: {
                "projection_digest": (
                    "1" * 64
                    if epoch["version_authority_high_water"] == 142
                    else "2" * 64
                )
            },
        )

        epoch, handoff, digest = route._stable_stream_projection(max_attempts=1)

        assert epoch["version_authority_high_water"] == 143
        assert handoff["projection_digest"] == "1" * 64
        assert digest is None

    def test_stream_projection_accepts_latest_handoff_revision_in_same_epoch(
        self,
        monkeypatch,
    ):
        import epoch_authority
        import server.routes.evolution as route

        projection = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "reset_receipt_valid": True,
            "reset_receipt_digest": "a" * 64,
            "version_authority_high_water": 143,
            "active_bots": ["national_v143"],
        }
        revision = {"value": 2}
        epoch_reads = {"count": 0}

        def load_epoch():
            epoch_reads["count"] += 1
            if epoch_reads["count"] == 2:
                # The journal may advance after its complete projection was
                # read.  That does not invalidate the observation bracketed by
                # an unchanged epoch.
                revision["value"] = 3
            return dict(projection)

        monkeypatch.setattr(route, "_epoch_projection", load_epoch)
        monkeypatch.setattr(
            route,
            "_handoff_projection",
            lambda _epoch: {
                "projection_digest": "2" * 64,
                "record_revision": revision["value"],
            },
        )

        epoch, handoff, digest = route._stable_stream_projection(max_attempts=1)

        assert epoch == projection
        assert handoff == {
            "projection_digest": "2" * 64,
            "record_revision": 2,
        }
        assert digest == epoch_authority.epoch_stream_authority_digest(
            projection
        )
        assert revision["value"] == 3

    def test_state_fails_closed_instead_of_returning_torn_epoch_handoff(
        self,
        client,
        monkeypatch,
    ):
        import server.routes.evolution as route
        from server.routes._helpers import post_publication_handoff_projection

        monkeypatch.setattr(
            route,
            "_stable_stream_projection",
            lambda: (
                {
                    "evaluation_epoch": "national_tcp_policy_v1",
                    "state": "strict_published",
                    "initialized": True,
                    "reset_receipt_valid": True,
                    "reset_receipt_digest": "a" * 64,
                    "version_authority_high_water": 143,
                    "current_v": 143,
                    "next_v": 144,
                    "active_bots": ["national_v143"],
                    "active_generation": None,
                },
                {"status": "pending", "projection_digest": "b" * 64},
                None,
            ),
        )
        monkeypatch.setattr(
            route,
            "_handoff_projection",
            lambda _epoch: post_publication_handoff_projection(enabled=False),
        )

        data = client.get("/api/evolution/state").json()

        assert data["epoch_state"] == "epoch_authority_unavailable"
        assert data["epoch_initialized"] is False
        assert data["stream_authority_digest"] is None
        assert data["post_publication_handoff"]["status"] == "none"

    def test_uninitialized_stream_does_not_subscribe_or_replay_ring(self, client, monkeypatch):
        import epoch_authority
        from server.app import broadcaster

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "reset_required",
                "initialized": False,
                "reset_receipt_digest": None,
                "current_v": 142,
                "next_v": 143,
            },
        )

        def forbidden_subscription(*_args, **_kwargs):
            raise AssertionError("pre-reset stream subscribed to broadcaster")

        monkeypatch.setattr(broadcaster, "add_client", forbidden_subscription)

        with client.stream("GET", "/api/evolution/stream") as response:
            body = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: epoch_blocked" in body
        assert "national_tcp_policy_v1" in body
        assert "event: stream_closed" in body

    def test_stale_expected_authority_is_fenced_before_subscription_or_replay(
        self,
        client,
        monkeypatch,
    ):
        import epoch_authority
        from server.app import broadcaster

        projection = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "reset_receipt_valid": True,
            "reset_receipt_digest": "a" * 64,
            "version_authority_high_water": 143,
            "active_bots": ["national_v143"],
            "current_v": 143,
            "next_v": 144,
        }
        current = epoch_authority.epoch_stream_authority_digest(projection)
        assert current is not None and current != "f" * 64
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: dict(projection),
        )

        def forbidden_subscription(*_args, **_kwargs):
            raise AssertionError("stale expected identity subscribed to replay ring")

        monkeypatch.setattr(broadcaster, "add_client", forbidden_subscription)
        broadcaster.bind_authority("f" * 64)
        broadcaster.broadcast("history", {"msg": "stale-ring-marker"})

        with client.stream(
            "GET",
            f"/api/evolution/stream?authority={'f' * 64}",
        ) as response:
            body = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: epoch_blocked" in body
        assert "stream_authority_mismatch" in body
        assert "stale-ring-marker" not in body
        assert broadcaster.get_stats()["authority_identity"] == current
