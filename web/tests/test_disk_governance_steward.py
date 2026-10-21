"""Disk-governance LLM steward: bounded whitelist executor + saturator seam.

The steward closes the loop designed in the 2026-10-21 disk-governance
investigation: a pressure-tier read-side classifier (ok/tight/critical), a
closed-door janitor-cycle detector, a create-once ``pending_request.json``
consumed by an explicit-priority saturator dispatch branch (zero tools,
``SATURATOR``-prefixed role so provider failures neither arm the durable
pause nor fall into the DEFAULT timeout bucket), and an executor that only
ever runs whitelisted tighten actions through the existing library entries
under the janitor's own locks, with hard clamp bounds, receipt-first
execution, and full-discard (event, no LLM retry) for forged / replayed /
out-of-bounds recommendations.

The three adversarial classes the ask requires coverage for:
forged (binding mismatch), replay (already-consumed request), and
out-of-bounds (unknown action / malformed parameter).
"""

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import disk_governance_steward as steward  # noqa: E402

GIB = 1024 * 1024 * 1024
MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit_event(results_dir: Path, row: dict) -> None:
    path = results_dir / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _hygiene_event(
    *, free_bytes: int, bytes_freed: int, ts: float | None = None
) -> dict:
    return {
        "ts": time.time() if ts is None else ts,
        "type": "pipeline.disk_hygiene_done",
        "severity": "warn",
        "message": "Disk hygiene reaped non-authority artifacts",
        "data": {
            "bytes_freed": bytes_freed,
            "removed": 0,
            "pressure": free_bytes < 4 * GIB,
            "free_bytes": free_bytes,
        },
    }


def _seed_closed_observations(results_dir: Path, count: int, *, tier: str = "tight") -> None:
    # Chronological append order (oldest first) — the ledger contract.
    closed = tier in {"tight", "critical"}
    rows = [
        {
            "schema_version": 1,
            "ts": time.time() - (count - 1 - i) * 300,
            "tier": tier,
            "closed": closed,
        }
        for i in range(count)
    ]
    path = steward.governance_dir(results_dir) / "observations.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


class EventCapture:
    def __init__(self, monkeypatch):
        self.events = []
        monkeypatch.setattr(
            "system_log.log_system_event", lambda t, s, m, d=None: self.events.append(
                {"type": t, "severity": s, "message": m, "data": d or {}}
            )
        )

    def of_type(self, event_type):
        return [e for e in self.events if e["type"] == event_type]


@pytest.fixture()
def events(monkeypatch):
    return EventCapture(monkeypatch)


def _create_request(results_dir: Path, **kwargs) -> dict:
    observation = kwargs.pop(
        "observation",
        {"ts": time.time(), "tier": "tight", "closed": True},
    )
    request = steward.create_review_request(results_dir, observation=observation)
    assert request and request.get("request_id")
    return request


def _llm_output(request: dict, action="hold", param_value=None, param_name=None) -> str:
    param_name = param_name or (
        "days" if action == "tighten_journal_retention" else "keep"
    )
    payload = {
        "request_id": request["request_id"],
        "packet_digest": request["packet_digest"],
        "action": action,
        "reason": "events backlog dominates; safe margin exhausted",
    }
    if param_value is not None:
        payload[param_name] = param_value
    return "```json\n" + json.dumps(payload) + "\n```"


async def _dispatch_and_record(results_dir: Path, request: dict, output: str) -> dict:
    claim = steward.claim_dispatch_slot(results_dir)
    assert claim and claim["request"]["request_id"] == request["request_id"]
    return steward.record_review_output(results_dir, request["request_id"], output)


# ---------------------------------------------------------------------------
# pressure tier classification (pure)
# ---------------------------------------------------------------------------


def test_pressure_tier_boundaries():
    min_free = 4 * GIB
    assert steward.classify_pressure_tier(5 * GIB, min_free) == "ok"
    assert steward.classify_pressure_tier(4 * GIB, min_free) == "ok"
    assert steward.classify_pressure_tier(4 * GIB - 1, min_free) == "tight"
    assert steward.classify_pressure_tier(2 * GIB, min_free) == "tight"
    assert steward.classify_pressure_tier(2 * GIB - 1, min_free) == "critical"
    assert steward.classify_pressure_tier(0, min_free) == "critical"
    assert steward.classify_pressure_tier(None, min_free) is None


# ---------------------------------------------------------------------------
# B2 (2026-10-21): the identity window is the OPERATOR env window the janitor
# reads (POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS, committed 4), never the bare
# library constant 20 — including the critical-tier cap 4.
# ---------------------------------------------------------------------------


def test_identity_window_reads_operator_env_and_critical_cap(monkeypatch):
    monkeypatch.delenv("POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS", raising=False)
    assert steward._identity_keep_window() == 20  # unset -> library fallback
    monkeypatch.setenv("POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS", "40")
    assert steward._identity_keep_window() == 40
    assert steward._identity_keep_window(tier="critical") == 4  # critical cap
    assert steward._identity_keep_window(tier="tight") == 40
    monkeypatch.setenv("POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS", "bogus")
    assert steward._identity_keep_window() == 20  # unparsable -> fallback
    monkeypatch.setenv("POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS", "0")
    assert steward._identity_keep_window() == 1  # >=1 clamp, same as janitor
    # Committed-production shape: window 4 below the steward floor 5 ->
    # the executor ceiling collapses onto the floor (values stay >= the
    # operator's own window: looser, never staging beyond operator intent).
    monkeypatch.setenv("POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS", "4")
    assert steward._identity_clamp_ceiling() == 5
    assert steward._identity_clamp_ceiling(tier="critical") == 5
    monkeypatch.setenv("POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS", "40")
    assert steward._identity_clamp_ceiling() == 40
    assert steward._identity_clamp_ceiling(tier="critical") == 5  # cap 4 < floor


def test_closed_door_identity_noop_uses_operator_env_window(tmp_path, monkeypatch):
    archive = tmp_path / "archive" / "evaluation_identity"
    archive.mkdir(parents=True)
    for i in range(10):
        (archive / f"202601{i + 1:02d}_000000").mkdir()
    _emit_event(tmp_path, _hygiene_event(free_bytes=3 * GIB, bytes_freed=0))
    monkeypatch.setenv("POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS", "4")
    obs = steward.observe_gate_state(tmp_path)
    # Committed window 4: 10 snapshots means the janitor still has staging
    # work — NOT a no-op, so the door is open (the pre-fix code read 20 and
    # wrongly judged within_budget).
    assert obs["windows"]["evaluation_identity"] == {
        "keep": 4, "snapshots": 10, "state": "over_budget",
    }
    assert obs["closed"] is False
    monkeypatch.setenv("POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS", "20")
    obs = steward.observe_gate_state(tmp_path)
    assert obs["windows"]["evaluation_identity"]["state"] == "within_budget"
    assert obs["closed"] is True


def test_identity_execution_ceiling_follows_operator_env(tmp_path, monkeypatch):
    calls = {}

    def fake_identity_retention(results_dir, *, triggered_by="", keep=20):
        calls["keep"] = keep
        return {"ok": True, "moved": [], "receipt_path": "r"}

    import evaluation_identity_retention as eir

    monkeypatch.setattr(eir, "run_evaluation_identity_retention", fake_identity_retention)
    monkeypatch.setenv("POK_EVALUATION_IDENTITY_KEEP_SNAPSHOTS", "40")
    request = _create_request(tmp_path)
    # The packet advertises the operator window as the ceiling, not library 20.
    identity_entry = next(
        entry
        for entry in request["packet"]["whitelist"]
        if entry["action"] == "tighten_identity_window"
    )
    assert identity_entry["range"] == [5, 40]
    asyncio.run(
        _dispatch_and_record(
            tmp_path, request, _llm_output(request, "tighten_identity_window", 99)
        )
    )
    executed = steward.consume_and_execute_recommendation(tmp_path)
    assert executed["ok"] is True
    assert calls["keep"] == 40  # operator env ceiling, never the library 20
    receipt = json.loads(
        (
            steward.governance_dir(tmp_path) / "receipts" / f"{request['request_id']}.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["requested_keep"] == 99
    assert receipt["effective_keep"] == 40


# ---------------------------------------------------------------------------
# closed-door observation (read-side janitor approximation)
# ---------------------------------------------------------------------------


def test_closed_door_requires_tier_pressure_small_freed_and_noop_windows(tmp_path):
    # Bare results dir: identity archive / journal db / cost ledger are all
    # absent, so every bounded-retention window is provably a no-op. The
    # hygiene event carries the tight tier with <1MiB freed -> closed door.
    _emit_event(tmp_path, _hygiene_event(free_bytes=3 * GIB, bytes_freed=0))
    obs = steward.observe_gate_state(tmp_path)
    assert obs["tier"] == "tight"
    assert obs["closed"] is True
    assert obs["windows"]["workflow_journal"]["state"] == "workflow_db_missing"
    assert obs["windows"]["evaluation_identity"]["state"] == "missing"
    assert obs["windows"]["cost_ledger"]["state"] == "ledger_missing"


def test_closed_door_fails_on_big_freed_ok_tier_over_budget_or_due_windows(tmp_path):
    # Janitor freed >=1MiB this cycle: the door is open (progress is real).
    _emit_event(tmp_path, _hygiene_event(free_bytes=3 * GIB, bytes_freed=2 * MIB))
    assert steward.observe_gate_state(tmp_path)["closed"] is False

    # ok tier never requests a review.
    _emit_event(tmp_path, _hygiene_event(free_bytes=6 * GIB, bytes_freed=0))
    assert steward.observe_gate_state(tmp_path)["closed"] is False

    # Identity archive over budget: the janitor WOULD move snapshots.
    (tmp_path / "archive" / "evaluation_identity").mkdir(parents=True)
    for i in range(25):
        (tmp_path / "archive" / "evaluation_identity" / f"202601{i + 1:02d}_000000").mkdir()
    _emit_event(tmp_path, _hygiene_event(free_bytes=3 * GIB, bytes_freed=0))
    obs = steward.observe_gate_state(tmp_path)
    assert obs["windows"]["evaluation_identity"]["state"] == "over_budget"
    assert obs["closed"] is False

    # Journal hygiene due (receipt older than its interval): not provably no-op.
    workflow = tmp_path / "workflow"
    workflow.mkdir(parents=True)
    (workflow / "events.sqlite3").write_bytes(b"x")
    receipts = workflow / "journal_retention_receipts"
    receipts.mkdir(parents=True)
    old = receipts / "20261020T000000-deadbeef.json"
    old.write_text("{}", encoding="utf-8")
    import os

    stamp = time.time() - 3 * 86400
    os.utime(old, (stamp, stamp))
    obs = steward.observe_gate_state(tmp_path)
    assert obs["windows"]["workflow_journal"]["state"] == "due"
    assert obs["closed"] is False


def test_closed_door_fails_without_fresh_hygiene_event(tmp_path):
    # A stale event (older than twice the observe interval) cannot prove the
    # current cycle freed nothing -> fail toward NOT requesting a review.
    _emit_event(
        tmp_path,
        _hygiene_event(free_bytes=3 * GIB, bytes_freed=0, ts=time.time() - 4000),
    )
    assert steward.observe_gate_state(tmp_path)["closed"] is False
    # And with no event at all.
    (tmp_path / "other").mkdir()
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert steward.observe_gate_state(fresh)["closed"] is False


def test_cost_ledger_present_paths_classify_without_error(tmp_path):
    """A present ledger must classify (recent rows -> nothing_to_compact,
    aged rows -> due); regression for the unbound-state bug the live
    read-only smoke caught (2026-10-21)."""
    ledger = tmp_path / "generation_cost_ledger.jsonl"
    recent = json.dumps({"ts": time.time() - 60, "kind": "generation"}) + "\n"
    ledger.write_text(recent, encoding="utf-8")
    _emit_event(tmp_path, _hygiene_event(free_bytes=3 * GIB, bytes_freed=0))
    obs = steward.observe_gate_state(tmp_path)
    assert obs["windows"]["cost_ledger"]["state"] == "nothing_to_compact"
    assert obs["closed"] is True  # tight + no freed bytes + all windows no-op

    aged = json.dumps({"ts": time.time() - 40 * 86400, "kind": "generation"}) + "\n"
    ledger.write_text(recent + aged, encoding="utf-8")
    obs = steward.observe_gate_state(tmp_path)
    assert obs["windows"]["cost_ledger"]["state"] == "due"
    assert obs["closed"] is False  # compaction would free bytes: door open


# ---------------------------------------------------------------------------
# request lifecycle: trigger gating, create-once, 24h interval
# ---------------------------------------------------------------------------


def test_request_created_after_three_closed_door_observations(tmp_path, events):
    _seed_closed_observations(tmp_path, 3)
    request = steward.maybe_create_review_request(tmp_path)
    assert request is not None
    path = steward.pending_request_path(tmp_path)
    assert path.is_file()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["request_id"] == request["request_id"]
    assert on_disk["state"] == "pending"
    assert on_disk["packet_digest"]
    assert on_disk["expires_at"] > on_disk["created_at"]
    assert len(on_disk["packet_digest"]) == 64
    requested = events.of_type("pipeline.disk_governance_review_requested")
    assert requested and requested[0]["data"]["request_id"] == request["request_id"]
    # Packet binds the observation that justified the request.
    assert on_disk["packet"]["tier"] == "tight"

    # Create-once: a second trigger call does not mint a second request.
    again = steward.maybe_create_review_request(tmp_path)
    assert again is None
    assert len(events.of_type("pipeline.disk_governance_review_requested")) == 1


def test_request_not_created_with_open_door_or_too_few_closed(tmp_path, events):
    _seed_closed_observations(tmp_path, 2)
    assert steward.maybe_create_review_request(tmp_path) is None
    _seed_closed_observations(tmp_path, 1, tier="ok")
    assert steward.maybe_create_review_request(tmp_path) is None
    assert events.of_type("pipeline.disk_governance_review_requested") == []


def test_request_interval_gate_blocks_within_24h(tmp_path, events):
    _seed_closed_observations(tmp_path, 3)
    first = steward.maybe_create_review_request(tmp_path)
    assert first is not None
    # Simulate the earlier request having been fully consumed long ago by
    # removing the pending file but leaving its receipt behind: the interval
    # gate must still hold (at most one review per interval window).
    steward.pending_request_path(tmp_path).unlink()
    receipts = steward.governance_dir(tmp_path) / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / f"{first['request_id']}.json").write_text("{}", encoding="utf-8")
    assert steward.maybe_create_review_request(tmp_path) is None


def test_expired_pending_request_swept_before_new_trigger(tmp_path, events):
    # No closed-door observations here: this test is focused on the expiry
    # sweep alone (a new request would legitimately follow in the live loop
    # once the interval gate and closed doors both hold again).
    request = _create_request(tmp_path)
    path = steward.pending_request_path(tmp_path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["created_at"] = time.time() - 2 * 86400
    on_disk["expires_at"] = time.time() - 86400
    path.write_text(json.dumps(on_disk), encoding="utf-8")
    summary = steward.run_steward_tick(tmp_path)
    assert summary["expired_pending"] >= 1
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["state"] == "abandoned"
    abandoned = events.of_type("pipeline.disk_governance_review_abandoned")
    assert abandoned and abandoned[-1]["data"]["reason"] == "expired"


# ---------------------------------------------------------------------------
# packet + prompt
# ---------------------------------------------------------------------------


def test_packet_bounded_and_carries_windows_and_whitelist(tmp_path):
    _seed_closed_observations(tmp_path, 3)
    _emit_event(tmp_path, _hygiene_event(free_bytes=3 * GIB, bytes_freed=0))
    request = _create_request(
        tmp_path, observation=steward.observe_gate_state(tmp_path)
    )
    raw = json.dumps(request["packet"], sort_keys=True).encode("utf-8")
    assert len(raw) <= steward.PACKET_MAX_BYTES
    packet = request["packet"]
    assert packet["tier"] == "tight"
    assert packet["consecutive_closed_door_cycles"] >= 3
    assert set(packet["windows"]) >= {
        "evaluation_identity",
        "workflow_journal",
        "candidate_ledger",
        "cost_ledger",
    }
    actions = {entry["action"] for entry in packet["whitelist"]}
    assert actions == {
        "tighten_identity_window",
        "tighten_journal_retention",
        "tighten_candidate_keep",
        "hold",
    }


def test_prompt_binds_ids_whitelist_and_single_json_contract(tmp_path):
    request = _create_request(tmp_path)
    prompt = steward.build_review_prompt(request["packet"])
    assert request["request_id"] in prompt
    assert request["packet_digest"] in prompt
    for action in (
        "tighten_identity_window",
        "tighten_journal_retention",
        "tighten_candidate_keep",
        "hold",
    ):
        assert action in prompt
    assert "single JSON object" in prompt
    assert "no tools" in prompt.lower()


# ---------------------------------------------------------------------------
# role contract + timeout bucket (blocking-item #1 without touching
# llm_query.py / llm_role_observability.py)
# ---------------------------------------------------------------------------


def test_governance_role_contract_zero_tools_saturator_prefix():
    import llm_query
    import llm_saturator  # registers the generic saturator role at import

    steward.register_disk_governance_role()
    assert steward.ROLE_NAME.startswith("SATURATOR")
    # Pause-persistence exemption (llm_query.py:1395-1412) keys on the prefix.
    assert llm_query._role_exempt_from_pause_persistence(steward.ROLE_NAME) is True
    contract = llm_query.resolve_llm_role_contract(steward.ROLE_NAME)
    assert contract.role_id == "llm_disk_governance"
    assert contract.allowed_tool_sets == (frozenset(),)
    # The generic saturator contract must still resolve for the research role.
    assert (
        llm_query.resolve_llm_role_contract("SATURATOR STRATEGY RESEARCH").role_id
        == "llm_saturator"
    )
    # Timeout policy: the SATURATOR substring branch (committed env
    # 900/900/600/7200), never the DEFAULT bucket.
    gov_policy = llm_query._role_timeout_policy(steward.ROLE_NAME)
    sat_policy = llm_query._role_timeout_policy("SATURATOR STRATEGY RESEARCH")
    assert gov_policy == sat_policy


# ---------------------------------------------------------------------------
# dispatch slot + attempt accounting
# ---------------------------------------------------------------------------


def test_claim_dispatch_slot_marks_dispatching(tmp_path):
    request = _create_request(tmp_path)
    claim = steward.claim_dispatch_slot(tmp_path)
    assert claim["request"]["request_id"] == request["request_id"]
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["state"] == "dispatching"
    assert on_disk["attempts"] == 1
    assert on_disk["last_attempt_at"] > 0
    # No second concurrent dispatch on the same request.
    assert steward.claim_dispatch_slot(tmp_path) is None


def test_attempt_accounting_provider_not_charged_and_abandon_cap(tmp_path, events):
    request = _create_request(tmp_path)
    rid = request["request_id"]

    # Provider-class failure (GLM 1302 body): not charged, back to pending.
    steward.claim_dispatch_slot(tmp_path)
    steward.note_dispatch_outcome(
        tmp_path,
        rid,
        error=RuntimeError(
            "Request rejected (429) · [1302][您的账户已达到速率限制，请您控制请求频率]"
        ),
    )
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["state"] == "pending"
    assert on_disk["charged_attempts"] == 0

    # Non-provider failures charge; three charged attempts abandon the request.
    for _ in range(3):
        steward.claim_dispatch_slot(tmp_path)
        steward.note_dispatch_outcome(tmp_path, rid, error=TimeoutError("stall"))
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["state"] == "abandoned"
    assert on_disk["charged_attempts"] == 3
    abandoned = events.of_type("pipeline.disk_governance_review_abandoned")
    assert abandoned and abandoned[-1]["data"]["reason"] == "attempt_cap"
    assert steward.has_dispatchable_request(tmp_path) is False


def test_stale_dispatching_request_reaped_by_tick(tmp_path):
    request = _create_request(tmp_path)
    steward.claim_dispatch_slot(tmp_path)
    path = steward.pending_request_path(tmp_path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    on_disk["last_attempt_at"] = time.time() - (steward.DISPATCH_STALE_SEC + 60)
    path.write_text(json.dumps(on_disk), encoding="utf-8")
    steward.run_steward_tick(tmp_path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["state"] == "pending"  # preempted/lost dispatch: reusable
    assert on_disk["charged_attempts"] == 1


# ---------------------------------------------------------------------------
# recommendation validation + execution (the three adversarial classes)
# ---------------------------------------------------------------------------


def test_valid_recommendation_records_and_executes_with_clamps(tmp_path, events, monkeypatch):
    calls = {}

    def fake_identity_retention(results_dir, *, triggered_by="", keep=20):
        calls["identity"] = {"keep": keep, "triggered_by": triggered_by}
        return {"ok": True, "moved": [{"name": "x"}], "receipt_path": "r"}

    import evaluation_identity_retention as eir

    monkeypatch.setattr(eir, "run_evaluation_identity_retention", fake_identity_retention)

    request = _create_request(tmp_path)
    result = asyncio.run(_dispatch_and_record(tmp_path, request, _llm_output(request, "tighten_identity_window", 2)))
    assert result["ok"] is True
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["state"] == "awaiting_execution"

    executed = steward.consume_and_execute_recommendation(tmp_path)
    assert executed["ok"] is True
    # LLM asked for keep=2; the hard floor clamps to 5 and the receipt records it.
    assert calls["identity"]["keep"] == 5
    assert calls["identity"]["triggered_by"] == "disk_governance_steward"
    receipt_path = steward.governance_dir(tmp_path) / "receipts" / f"{request['request_id']}.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["action"] == "tighten_identity_window"
    assert receipt["requested_keep"] == 2
    assert receipt["effective_keep"] == 5
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["state"] == "consumed"
    done = events.of_type("pipeline.disk_governance_recommendation_executed")
    assert done and done[0]["data"]["effective_keep"] == 5


def test_ceiling_clamps_and_journal_and_candidate_actions(tmp_path, monkeypatch):
    calls = {}

    def fake_journal_hygiene(results_dir, **kwargs):
        calls["journal"] = dict(kwargs)
        return {"ok": True, "bytes_freed": 0, "removed": 0, "skipped": "interval"}

    def fake_compact(path=None, *, keep_versions=50):
        calls["candidate"] = {"keep_versions": keep_versions}
        return {"receipt_persisted": True}

    import candidate_store
    import workflow_journal_hygiene as wjh

    monkeypatch.setattr(wjh, "run_workflow_journal_hygiene", fake_journal_hygiene)
    monkeypatch.setattr(candidate_store, "compact_candidate_ledger", fake_compact)

    request = _create_request(tmp_path)
    asyncio.run(_dispatch_and_record(tmp_path, request, _llm_output(request, "tighten_journal_retention", 99, param_name="days")))
    executed = steward.consume_and_execute_recommendation(tmp_path)
    assert executed["ok"] is True
    # Ceiling clamp: retention cannot exceed the configured 30-day window.
    assert calls["journal"]["retention_sec"] == pytest.approx(30 * 86400)
    assert calls["journal"]["retention_sec"] <= 30 * 86400
    receipt = json.loads(
        (steward.governance_dir(tmp_path) / "receipts" / f"{request['request_id']}.json").read_text(encoding="utf-8")
    )
    assert receipt["requested_days"] == 99
    assert receipt["effective_days"] == 30
    # The library's own skip code is recorded verbatim (no invention).
    assert receipt["outcome"]["skipped"] == "interval"

    request2 = _create_request(tmp_path)
    (tmp_path / "candidates.jsonl").write_text("{}", encoding="utf-8")
    asyncio.run(_dispatch_and_record(tmp_path, request2, _llm_output(request2, "tighten_candidate_keep", 3)))
    executed2 = steward.consume_and_execute_recommendation(tmp_path)
    assert executed2["ok"] is True
    assert calls["candidate"]["keep_versions"] == 10


def test_hold_action_writes_receipt_without_any_tighten(tmp_path, events, monkeypatch):
    def boom(*args, **kwargs):  # any tighten execution must fail the test
        raise AssertionError("hold must not execute any tighten")

    import evaluation_identity_retention as eir

    monkeypatch.setattr(eir, "run_evaluation_identity_retention", boom)
    request = _create_request(tmp_path)
    asyncio.run(_dispatch_and_record(tmp_path, request, _llm_output(request, "hold", None)))
    executed = steward.consume_and_execute_recommendation(tmp_path)
    assert executed["ok"] is True
    receipt = json.loads(
        (steward.governance_dir(tmp_path) / "receipts" / f"{request['request_id']}.json").read_text(encoding="utf-8")
    )
    assert receipt["action"] == "hold"


def test_forged_recommendation_rejected_without_execution(tmp_path, events, monkeypatch):
    """伪造: a hand-written sidecar whose binding does not match the live
    request (wrong packet digest) is discarded whole with an event; no
    tighten executes and the request returns to pending (unconsumed)."""

    def boom(*args, **kwargs):
        raise AssertionError("forged recommendation must not execute")

    import evaluation_identity_retention as eir

    monkeypatch.setattr(eir, "run_evaluation_identity_retention", boom)

    request = _create_request(tmp_path)
    # Plant a forged sidecar bound to the real request id but a foreign digest.
    forged = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "packet_digest": "f" * 64,
        "action": "tighten_identity_window",
        "keep": 5,
        "recommendation_digest": "0" * 64,
    }
    sidecar = (
        steward.governance_dir(tmp_path)
        / f"recommendation-{request['request_id']}.json"
    )
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps(forged), encoding="utf-8")
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    on_disk["state"] = "awaiting_execution"
    steward.pending_request_path(tmp_path).write_text(json.dumps(on_disk), encoding="utf-8")

    outcome = steward.consume_and_execute_recommendation(tmp_path)
    assert outcome["ok"] is False
    assert outcome["class"] == "forged_binding_mismatch"
    rejected = events.of_type("pipeline.disk_governance_recommendation_rejected")
    assert rejected and rejected[0]["data"]["class"] == "forged_binding_mismatch"
    # The raw forged payload is not echoed back through the event.
    assert json.dumps(forged) not in json.dumps(events.events)
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["state"] == "pending"
    # The forged sidecar itself is discarded.
    assert not sidecar.exists()


def test_replayed_recommendation_executes_exactly_once(tmp_path, events, monkeypatch):
    """重放: after a request is consumed, re-presenting an identical valid
    recommendation (or re-running the executor) never re-executes the tighten."""

    calls = {"n": 0}

    def fake_identity_retention(results_dir, *, triggered_by="", keep=20):
        calls["n"] += 1
        return {"ok": True, "moved": [], "receipt_path": "r"}

    import evaluation_identity_retention as eir

    monkeypatch.setattr(eir, "run_evaluation_identity_retention", fake_identity_retention)

    request = _create_request(tmp_path)
    asyncio.run(_dispatch_and_record(tmp_path, request, _llm_output(request, "tighten_identity_window", 5)))
    first = steward.consume_and_execute_recommendation(tmp_path)
    assert first["ok"] is True
    assert calls["n"] == 1

    # Replay: the consumed request plus a byte-identical recommendation file.
    sidecar = (
        steward.governance_dir(tmp_path)
        / f"recommendation-{request['request_id']}.json"
    )
    assert sidecar.exists()
    replay = steward.consume_and_execute_recommendation(tmp_path)
    assert replay["ok"] is False
    assert replay["class"] in {"replay", "no_request"}
    rejected = events.of_type("pipeline.disk_governance_recommendation_rejected")
    assert any(e["data"]["class"] == "replay" for e in rejected)
    assert calls["n"] == 1


def test_out_of_bounds_recommendation_discarded_whole_no_retry(tmp_path, events, monkeypatch):
    """越界: unknown action / wrong-type parameter / foreign request binding
    are discarded whole with an event; the LLM is never re-dispatched from
    the discard path, and no tighten executes."""

    def boom(*args, **kwargs):
        raise AssertionError("out-of-bounds recommendation must not execute")

    import evaluation_identity_retention as eir

    monkeypatch.setattr(eir, "run_evaluation_identity_retention", boom)

    # 1. Unknown (dangerous-sounding) action outside the enum.
    request = _create_request(tmp_path)
    steward.claim_dispatch_slot(tmp_path)
    outcome = steward.record_review_output(
        tmp_path, request["request_id"], _llm_output(request, "purge_events_jsonl", 1)
    )
    assert outcome["ok"] is False
    assert outcome["class"] == "out_of_bounds"
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["state"] == "pending"  # no recommendation recorded
    assert on_disk["charged_attempts"] == 1  # the attempt is consumed
    rejected = events.of_type("pipeline.disk_governance_recommendation_rejected")
    assert rejected[-1]["data"]["class"] == "out_of_bounds"
    assert "purge_events_jsonl" in json.dumps(rejected[-1])  # action name, not raw text

    # 2. Wrong-type parameter (string keep) on a fresh request.
    request = _create_request(tmp_path)
    steward.claim_dispatch_slot(tmp_path)
    outcome = steward.record_review_output(
        tmp_path, request["request_id"], _llm_output(request, "tighten_identity_window", "5")
    )
    assert outcome["ok"] is False
    assert outcome["class"] == "out_of_bounds"

    # 3. Output echoing a foreign request binding.
    request = _create_request(tmp_path)
    steward.claim_dispatch_slot(tmp_path)
    outcome = steward.record_review_output(
        tmp_path,
        request["request_id"],
        json.dumps({"request_id": "deadbeef", "packet_digest": "0" * 64, "action": "hold"}),
    )
    assert outcome["ok"] is False
    assert outcome["class"] == "forged_binding_mismatch"

    # 4. Non-JSON / non-object output.
    request = _create_request(tmp_path)
    steward.claim_dispatch_slot(tmp_path)
    outcome = steward.record_review_output(tmp_path, request["request_id"], "I cannot help with that.")
    assert outcome["ok"] is False
    assert outcome["class"] == "malformed_json"

    # No recommendation sidecar was ever written for the last request.
    assert not (
        steward.governance_dir(tmp_path) / f"recommendation-{request['request_id']}.json"
    ).exists()
    # No receipt -> nothing executed.
    assert not (steward.governance_dir(tmp_path) / "receipts").exists()


# ---------------------------------------------------------------------------
# steward tick smoke + saturator seam
# ---------------------------------------------------------------------------


def test_run_steward_tick_smoke_on_bare_dir(tmp_path):
    summary = steward.run_steward_tick(tmp_path)
    assert isinstance(summary, dict)
    obs = steward.governance_dir(tmp_path) / "observations.jsonl"
    assert obs.is_file()
    assert summary["observation_appended"] is True
    # No hygiene event -> door not provably closed -> no request.
    assert summary["request_created"] is False
    assert not steward.pending_request_path(tmp_path).exists()


def test_saturator_session_prefers_governance_and_writes_no_findings(monkeypatch, tmp_path):
    """B1 (2026-10-21): the governance dispatch shares _one_saturator_session's
    ALREADY-REGISTERED provider call point — no new run_claude_query /
    render_llm_prompt call site (the registry inventory stays at its
    human-reviewed 21 entries). The claimed request is dispatched zero-tool
    under the SATURATOR-prefixed role and its output recorded through the
    steward; findings.jsonl is never touched."""
    import llm_saturator

    monkeypatch.setattr("evolution_infra.RESULTS_DIR", tmp_path)
    request = _create_request(tmp_path)

    seen = {}

    async def fake_run_claude_query(*args, **kwargs):
        seen["role"] = args[3]
        seen["tools"] = kwargs.get("tools")
        return (_llm_output(request, "hold"), 0.0, {"input_tokens": 5})

    monkeypatch.setattr("llm_query.run_claude_query", fake_run_claude_query)
    result = asyncio.run(llm_saturator._one_saturator_session(7))
    assert result["job"] == "disk_governance"
    assert result["recorded"] is True
    assert seen["role"] == steward.ROLE_NAME
    assert seen["tools"] == []
    # The steward consumed the recorded output (not findings).
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["state"] == "awaiting_execution"
    assert on_disk["attempts"] == 1
    # The governance branch must never touch the findings.jsonl evidence face.
    assert not (tmp_path / "saturator" / "findings.jsonl").exists()


def test_saturator_session_governance_failure_noted_without_charging_provider(monkeypatch, tmp_path):
    """A provider-class failure of the claimed review is noted through the
    steward (not charged) and surfaces as a failed session like any other."""
    import llm_saturator

    monkeypatch.setattr("evolution_infra.RESULTS_DIR", tmp_path)
    request = _create_request(tmp_path)

    async def fake_run_claude_query(*args, **kwargs):
        raise RuntimeError(
            "Request rejected (429) · [1302][您的账户已达到速率限制，请您控制请求频率]"
        )

    monkeypatch.setattr("llm_query.run_claude_query", fake_run_claude_query)
    # Keep the saturator's module-level backoff timers out of other tests.
    monkeypatch.setattr(llm_saturator, "_quota_pause_until", 0.0)
    monkeypatch.setattr(llm_saturator, "_fail_pause_until", 0.0)
    monkeypatch.setattr(llm_saturator, "_rate_limit_streak", 0)
    result = asyncio.run(llm_saturator._one_saturator_session(8))
    assert result["ok"] is False
    on_disk = json.loads(steward.pending_request_path(tmp_path).read_text(encoding="utf-8"))
    assert on_disk["state"] == "pending"
    assert on_disk["charged_attempts"] == 0  # provider-class: not charged
    assert on_disk["attempts"] == 1


def test_saturator_session_falls_through_when_no_request(monkeypatch, tmp_path):
    import llm_saturator

    monkeypatch.setattr("evolution_infra.RESULTS_DIR", tmp_path)
    monkeypatch.setattr(steward, "has_dispatchable_request", lambda _rd: False)

    async def fake_run_claude_query(*args, **kwargs):
        return ("some output", 0.0, {"input_tokens": 5, "output_tokens": 1})

    monkeypatch.setattr("llm_query.run_claude_query", fake_run_claude_query)
    monkeypatch.setattr(llm_saturator, "_saturator_bots", lambda _sid, limit=2: [])
    result = asyncio.run(llm_saturator._one_saturator_session(3))
    assert result["ok"] is True
    assert result.get("job") in {"matchup_packet", "line_audit", "function_trace"}
