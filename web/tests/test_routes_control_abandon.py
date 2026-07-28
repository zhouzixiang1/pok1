"""Tests for POST /api/control/abandon.

The abandon endpoint is an operator-facing escape hatch for a generation
stuck at an abandonable stage (e.g. ``workers_done`` / ``rework_running``).
It stops the live orchestrator inside the runtime lifecycle lock and then
calls the canonical abandon transaction (``_do_abandon_generation``).

These tests do not exercise real publication-authority code. They mock the
checkpoint reader, the disposable-stage guard
(``control._abandonable_stage_block``), and ``_do_abandon_generation`` to
verify the HTTP contract: the no-active-generation 409, the
disposable-stage guard, the CAS-mismatch translation, and that the
orchestrator task is stopped before the canonical abandon runs.

The classification of which stages are disposable is owned by
``pipeline_state.generic_abandon_block`` and exercised in its own test
suite; here we verify the route consults that guard and translates its
refusal into a typed ``stage_not_disposable`` 409.
"""

import pytest


def _install_checkpoint(monkeypatch, checkpoint):
    """Replace the canonical checkpoint reader with a fixed projection.

    The control route reads through ``evolution_infra.read_pipeline_checkpoint``
    and gates the read on ``os.path.lexists(PIPELINE_STATE_FILE)``. Drive both
    so a test can express "no checkpoint" (``None``) without touching disk.
    """

    import evolution_infra
    import server.routes.control as control

    monkeypatch.setattr(
        evolution_infra,
        "read_pipeline_checkpoint",
        lambda: checkpoint,
    )
    # The route imports both symbols lazily inside the transaction via
    # ``from evolution_infra import ...``; they resolve to the same module
    # objects patched above. ``os.path.lexists`` gates the read, so reflect
    # presence there.
    present = checkpoint is not None
    monkeypatch.setattr(
        control.os.path,
        "lexists",
        lambda path: present if path is evolution_infra.PIPELINE_STATE_FILE else True,
    )


def _install_stage_guard(monkeypatch, block=None):
    """Replace ``control._abandonable_stage_block`` with a fixed outcome.

    ``None`` means the stage is disposable (proceed to abandon); a dict is
    the canonical refusal payload (return ``stage_not_disposable``).
    """

    import server.routes.control as control

    monkeypatch.setattr(
        control,
        "_abandonable_stage_block",
        lambda checkpoint, reason: block,
    )


def _install_abandon(monkeypatch, *, result=None, raise_exc=None, capture=None):
    """Replace ``_do_abandon_generation`` with a deterministic stub.

    The route imports this lazily from ``tool_bot_management`` inside the
    transaction, so patch it on its origin module.
    """

    import tool_bot_management

    async def fake_do_abandon(reason="abandon_generation", **kwargs):
        if capture is not None:
            capture["reason"] = reason
            capture["kwargs"] = kwargs
        if raise_exc is not None:
            raise raise_exc
        if callable(result):
            return result(reason=reason, **kwargs)
        return dict(result or {})

    monkeypatch.setattr(
        tool_bot_management,
        "_do_abandon_generation",
        fake_do_abandon,
    )


class TestAbandonRequiresActiveGeneration:
    def test_no_active_checkpoint_returns_409(self, client, monkeypatch):
        _install_checkpoint(monkeypatch, None)
        _install_stage_guard(monkeypatch, block=None)
        invoked = {"count": 0}
        _install_abandon(
            monkeypatch,
            result=lambda **_kw: invoked.__setitem__("count", invoked["count"] + 1) or {"abandoned": True},
        )

        resp = client.post("/api/control/abandon")

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "no_active_generation_to_abandon"
        assert detail["operation"] == "control_abandon_generation"
        assert detail["checkpoint_present"] is False
        # Nothing to abandon: the guard and the canonical transaction must
        # never run.
        assert invoked["count"] == 0


class TestAbandonSuccess:
    def test_abandon_at_disposable_stage_succeeds(self, client, monkeypatch):
        # ``workers_done`` is the canonical stuck-stage motivating this
        # endpoint: it is abandonable but never auto-routed by the cycle
        # timeout. The disposable classification itself is owned by
        # ``generic_abandon_block`` and tested elsewhere; here we let the
        # guard pass (block=None) and verify the route invokes the
        # canonical transaction and surfaces its receipt.
        checkpoint = {
            "stage": "workers_done",
            "next_v": 999,
            "source_v": 998,
            "checkpoint_revision": 7,
            "run_id": "run-999#1",
            "workflow_run_id": "run-999#1",
        }
        _install_checkpoint(monkeypatch, checkpoint)
        _install_stage_guard(monkeypatch, block=None)
        capture: dict = {}
        _install_abandon(
            monkeypatch,
            capture=capture,
            result={
                "abandoned": True,
                "cleared_checkpoint": True,
                "removed_directory": "national_v999",
                "reason": "abandon_generation",
                "abandoned_v": 999,
                "workflow_fenced": True,
                "workflow_run_id": "run-999#1",
                "abandon_transaction_id": "txn-abc",
                "abandon_receipt_digest": "deadbeef",
                "finalize_receipt_digest": "cafef00d",
            },
        )

        resp = client.post("/api/control/abandon")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "abandoned"
        assert body["operation"] == "control_abandon_generation"
        assert body["transaction_id"] == "txn-abc"
        assert body["abandoned_v"] == 999
        assert body["abandon_receipt_digest"] == "deadbeef"
        assert body["runtime_stopped"] is True
        # The canonical abandon receives the operator's default reason.
        assert capture["reason"] == "abandon_generation"

    def test_abandon_forwards_operator_reason_into_receipt(self, client, monkeypatch):
        checkpoint = {
            "stage": "rework_running",
            "next_v": 1000,
            "source_v": 999,
            "checkpoint_revision": 3,
            "run_id": "run-1000#1",
            "workflow_run_id": "run-1000#1",
        }
        _install_checkpoint(monkeypatch, checkpoint)
        _install_stage_guard(monkeypatch, block=None)
        capture: dict = {}
        _install_abandon(
            monkeypatch,
            capture=capture,
            result=lambda reason, **kw: {
                "abandoned": True,
                "cleared_checkpoint": True,
                "reason": reason,
                "abandoned_v": 1000,
                "abandon_transaction_id": "txn-reason",
            },
        )

        resp = client.post(
            "/api/control/abandon", json={"reason": "operator_rework_loop_stuck"}
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["reason"] == "operator_rework_loop_stuck"
        assert capture["reason"] == "operator_rework_loop_stuck"


class TestAbandonStageGuard:
    def test_never_disposable_stage_returns_409(self, client, monkeypatch):
        # ``verified`` is in the canonical ``never_disposable`` set in
        # ``pipeline_state.generic_abandon_block``: certification/publication
        # authority that must resume its own owner. The route must surface
        # that guard refusal as ``stage_not_disposable`` and never invoke
        # the canonical publication-authority transaction.
        checkpoint = {
            "stage": "verified",
            "next_v": 1001,
            "source_v": 1000,
            "checkpoint_revision": 5,
        }
        _install_checkpoint(monkeypatch, checkpoint)
        _install_stage_guard(
            monkeypatch,
            block={
                "abandoned": False,
                "blocked": True,
                "reason": "publication_or_certification_stage_not_disposable",
                "stage": "verified",
                "next_v": 1001,
                "source_v": 1000,
                "directive": "Refusing abandon for v1001 at non-disposable stage.",
            },
        )
        invoked = {"count": 0}
        _install_abandon(
            monkeypatch,
            result=lambda **_kw: invoked.__setitem__("count", invoked["count"] + 1) or {"abandoned": True},
        )

        resp = client.post("/api/control/abandon")

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "stage_not_disposable"
        assert detail["stage"] == "verified"
        assert detail["next_v"] == 1001
        assert detail["directive"].startswith("Refusing abandon")
        # The guard must refuse BEFORE the canonical transaction runs.
        assert invoked["count"] == 0


class TestAbandonStopsOrchestrator:
    def test_live_task_is_stopped_before_abandon_runs(self, client, monkeypatch):
        from server.state import app_state
        import server.routes.control as control

        # The endpoint stops the live orchestrator task inside the lifecycle
        # lock BEFORE invoking the canonical publication-authority abandon.
        # We assert the ordering by recording every state mutation the route
        # performs: ``request_shutdown`` first, then ``stop_running``, and
        # only then does ``_do_abandon_generation`` observe a stopped
        # runtime. We deliberately avoid planting a real asyncio task:
        # ``TestClient`` runs the request on its own portal loop, so a task
        # created on a separate loop would fail ``asyncio.shield`` with a
        # cross-loop ``RuntimeError`` unrelated to the contract under test.
        order: list[str] = []

        class DoneFakeTask:
            def done(self):
                return True

            def cancelled(self):
                return False

            def cancel(self):  # pragma: no cover - already done
                return False

        monkeypatch.setattr(
            app_state, "request_shutdown", lambda: order.append("request_shutdown")
        )
        monkeypatch.setattr(
            app_state, "stop_running", lambda: (order.append("stop_running"), DoneFakeTask())[1]
        )
        # ``app_state.to_dict()["running"]`` is consulted after the abandon;
        # force it to reflect the stopped runtime.
        monkeypatch.setattr(app_state, "to_dict", lambda: {"running": False})

        checkpoint = {
            "stage": "workers_done",
            "next_v": 1002,
            "source_v": 1001,
            "checkpoint_revision": 2,
        }
        _install_checkpoint(monkeypatch, checkpoint)
        _install_stage_guard(monkeypatch, block=None)

        observed: dict = {}

        def result(**_kwargs):
            observed["order_at_abandon"] = list(order)
            return {
                "abandoned": True,
                "cleared_checkpoint": True,
                "reason": "abandon_generation",
                "abandoned_v": 1002,
                "abandon_transaction_id": "txn-stop-first",
            }

        _install_abandon(monkeypatch, result=result)

        resp = client.post("/api/control/abandon")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["runtime_stopped"] is True
        # The canonical abandon observed the runtime already torn down.
        assert observed["order_at_abandon"] == ["request_shutdown", "stop_running"]
        # And the final runtime state is stopped.
        assert app_state.to_dict()["running"] is False


class TestAbandonCasMismatch:
    def test_canonical_cas_mismatch_surfaces_as_409(self, client, monkeypatch):
        checkpoint = {
            "stage": "workers_done",
            "next_v": 1003,
            "source_v": 1002,
            "checkpoint_revision": 9,
        }
        _install_checkpoint(monkeypatch, checkpoint)
        _install_stage_guard(monkeypatch, block=None)
        # The canonical abandon returns a dict (not raises) when the
        # workflow fence detects a CAS drift mid-transaction.
        _install_abandon(
            monkeypatch,
            result={
                "abandoned": False,
                "reason": "expected_checkpoint_identity_mismatch",
                "action": "stale_rejection_ignored",
            },
        )

        resp = client.post("/api/control/abandon")

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "checkpoint_cas_mismatch"
        assert detail["canonical_reason"] == "expected_checkpoint_identity_mismatch"
        assert detail["stage"] == "workers_done"


class TestAbandonCapabilityCatalog:
    def test_abandon_advertised_in_capability_catalog(self, client):
        resp = client.get("/api/control/tools")
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["capabilities"]}
        # The capability id is distinct from the MCP tool name
        # ``abandon_generation``: the HTTP registry is deliberately not a
        # projection of the orchestrator's MCP ``all_tools`` registry.
        assert "abandon_active_generation" in ids
        assert "abandon_generation" not in ids
        paths = {item["path"] for item in resp.json()["capabilities"]}
        assert "/api/control/abandon" in paths
