"""2026-10-09 复审修订：压制形态（suppressed divergence）才是 v532 真实根因.

对抗审计（issues-found，全部反例已用真实 store/journal 复现）裁定原 F-A 修复
误诊了事故形态，本文件钉住六项修订的契约：

* **F-H（决定性）**：生产 journal（``events.sqlite3`` run
  ``generation:532:workflow-v1`` seq 37 ``EffectDeferred``）证明冻结快照是
  *异常路径形状*（``exc.pause_state()`` 入射投影：有 ``observed_at``、无
  ``auto_resume_at``），且 1.5 秒后 store 的 ACTIVE 记录 digest
  （``7a1853d7…``，events.jsonl:431902）≠ 冻结 digest（``db4c64ce…``）。
  机制：异常路径先冻结再 persist；persist 落在同类别 ACTIVE 记录走压制分支
  （``llm_availability_store.py`` 同优先级同类别分支），store 保持旧 digest，
  入射 digest 只进 ``last_suppressed_evidence_digest`` —— 永远不成为记录
  digest、永远没有匹配回执，**任何 cap 无效**。修订：识别压制形态 ——
  非活动 audit 记录的 ``last_suppressed_evidence_digest`` 链（当前记录 +
  归档回执投影携带的 marker）与冻结 digest 匹配时授权，且该形态**不要求
  归档满容量**。事故时刻（05:09:04，归档仅 2 条）即恢复。
* **F-M1**：容量基授权与冻结 digest 无结构绑定（伪造 digest × 满 64 条归档
  即放行）。修订：删除容量基授权；授权只与压制链 digest 匹配结构性绑定。
* **F-M2**：快照/压制授权此前零审计痕迹。修订：授权恢复发独立系统事件
  ``pipeline.worker_resume_snapshot_authorized``（effect_id / 冻结 digest /
  授权形态 / 归档深度 / horizon），先例
  ``pipeline.crossover_degraded_to_parent_copy``。
* **F-M3**：裸 ``stop_running``（无 shutdown/cancel）停车后慢道静默复活；
  停车期 wrapper 存活使 ``/start`` 409 already_owned。修订：慢道每次睡眠前
  re-arm ``running``，每次尝试前校验 ``running`` 未被置 False（被清即视为
  外部停止意图，不再复活并退出监督器）；告警文案写明 Stop-then-Start 契约。
* **F-L**：``system_resume_horizon`` 的 quota 分支用 ``_parse_time``
  （naive→UTC）而 store 权威用 ``_parse_provider_reset_time``（naive→宿主
  本地），本机 +8h 偏差。修订：改用权威解析并对齐测试。

测试中的冻结记录逐字节内嵌 journal seq 37 原文（真实生产形状），store 侧
全部通过真实 store API 构造（isolated RESULTS_DIR，不触碰生产状态）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import logging

import pytest

import evolution_infra
from llm_availability import (
    QUOTA_429,
    SERVICE_UNAVAILABLE,
    build_llm_pause_state,
    classify_llm_availability,
)
import llm_availability_store as store
import server.state as state_module
import system_log
import tool_planning_worker  # noqa: F401  (parent-first import order)
import tool_planning_worker_durable
import tool_planning_worker_phases
from tool_planning_worker_durable import _worker_availability_resume_receipt_errors


# ---------------------------------------------------------------------------
# 生产 journal 原文 fixture（events.sqlite3 workflow_events, run
# generation:532:workflow-v1, seq 37, EffectDeferred —— 逐字节拷贝）。
# ---------------------------------------------------------------------------
V532_SEQ37_AVAILABILITY = {
    "active": True,
    "category": "service_unavailable",
    "evidence_digest": (
        "db4c64ce960a1ad3212af1c67871ae8c521149a9a247672171e261e45b21f19c"
    ),
    "http_status": None,
    "observed_at": "2026-10-08T21:06:56.041982+00:00",
    "persistent_pause": True,
    "provider_reset_at": None,
    "quota_reset_authority": None,
    "requires_manual_resume": False,
    "retry_policy": "bounded_backoff",
    "role": "WORKER auto_precommit_repair_policy_py (Strategic Regression Repair Architect)",
    "schema_version": 1,
    "source": "llm_availability",
    "summary": "provider rate limit; reduce request frequency",
}
#: 事故时刻 store 侧 ACTIVE 记录 digest（events.jsonl:431902，
#: orchestrator.llm_availability_cooldown —— 与冻结值分歧 1.5s 后仍未变）。
V532_STORE_DIGEST_AT_FREEZE = (
    "7a1853d7871d4453ccde108e9e5cdab807c82687e56a9a5d836d3db5ae2b51f7"
)
#: 第一次复活校验失败的事故时刻（21:09:04.987771Z）。
INCIDENT_MOMENT = datetime(2026, 10, 8, 21, 9, 4, 987771, tzinfo=timezone.utc)

BASE = datetime(2026, 10, 8, 21, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.delenv(store.RESUME_ENV, raising=False)
    return tmp_path


def _blip(text: str):
    issue = classify_llm_availability([text], statuses=[529])
    assert issue is not None and issue.category == SERVICE_UNAVAILABLE
    return issue


def _history_len(audit: dict | None) -> int:
    return len(
        [
            entry
            for entry in ((audit or {}).get("resume_receipt_history") or [])
            if isinstance(entry, dict)
        ]
    )


def _reconcile(audit_time: datetime):
    """Reconcile the live record inactive at ``audit_time`` (if due)."""
    record = store.load_llm_pause()
    due = store._parse_time((record or {}).get("auto_resume_at")) if record else None
    if due is None or audit_time > due:
        assert store.active_llm_pause(now=audit_time) is None
    return store.load_llm_pause()


def _build_incident_shape(moment_of_freeze: datetime | None = None):
    """重建生产事故形态：压制分歧 + 归档远未满。

    1. 两条 pre-wedge 锯齿（05:01-05:06 风暴已有 2 条回执）；
    2. 一条 ACTIVE 同类别记录（store digest ≠ 冻结 digest —— 生产为
       ``7a1853d7…``，这里用任意真实 blip，形状等价）；
    3. Worker 异常路径把 *自己的* blip 冻结进 journal（seq 37 原文）再
       persist → 压制分支：store 保持旧 digest，冻结 digest 只进
       ``last_suppressed_evidence_digest``；
    4. 记录冷却到点 → reconcile → audit inactive（marker 仍在当前记录上）。
    """
    moment_of_freeze = moment_of_freeze or datetime(
        2026, 10, 8, 21, 6, 56, 41982, tzinfo=timezone.utc
    )

    # 1. pre-wedge sawtooth: two archived receipts (like 05:01/05:04).
    t = BASE
    for index in range(2):
        store.persist_llm_pause(_blip(f"HTTP 529 GLM storm pre-wedge blip {index}"), now=t)
        _reconcile(t + timedelta(seconds=30))
        t = t + timedelta(seconds=32)

    # 2. the ACTIVE same-category store record (digest A, occurrences 1).
    store.persist_llm_pause(_blip("HTTP 529 GLM storm active record A"), now=t)
    active = store.load_llm_pause()
    assert active["active"] is True

    # 3. the Worker exception-path freeze (journal seq 37 verbatim) persisted
    #    onto the still-active record -> suppression divergence.
    freeze = dict(V532_SEQ37_AVAILABILITY)
    persisted = store.persist_llm_pause(freeze, now=moment_of_freeze)
    assert persisted["evidence_digest"] != freeze["evidence_digest"], (
        "suppression must keep the old (store) digest"
    )
    assert (
        persisted.get("last_suppressed_evidence_digest")
        == freeze["evidence_digest"]
    ), "the frozen (incident) digest must land only in the suppression marker"

    # 4. the suppressed record's cooldown elapses -> audit inactive.
    due = store._parse_time(persisted["auto_resume_at"])
    audit = _reconcile(due + timedelta(seconds=1))
    assert audit["active"] is False
    return freeze, audit


def _sawtooth_until_full(start: datetime, *, cap: int) -> datetime:
    """Drive persist+reconcile cycles until the retained history hits ``cap``."""
    moment = start
    while _history_len(store.load_llm_pause()) < cap:
        store.persist_llm_pause(
            _blip(f"HTTP 529 GLM storm continued blip {moment.isoformat()}"), now=moment
        )
        record = store.load_llm_pause()
        due = store._parse_time(record["auto_resume_at"])
        moment = due + timedelta(seconds=1)
        assert store.active_llm_pause(now=moment) is None
    return moment


def _sawtooth_cycles(start: datetime, cycles: int) -> datetime:
    """Drive exactly ``cycles`` persist+reconcile archive cycles.

    The retained history is bounded by the store cap, so a fill target above
    the cap can never be observed through the retained length; counting the
    cycles directly lets a test push the OLDEST receipts past the FIFO edge.
    """
    moment = start
    for index in range(cycles):
        store.persist_llm_pause(
            _blip(f"HTTP 529 GLM storm overflow blip {index}"), now=moment
        )
        record = store.load_llm_pause()
        due = store._parse_time(record["auto_resume_at"])
        moment = due + timedelta(seconds=1)
        assert store.active_llm_pause(now=moment) is None
    return moment


# ---------------------------------------------------------------------------
# F-H：压制形态在事故时刻即恢复（不等归档满容量）
# ---------------------------------------------------------------------------


def test_incident_suppression_shape_recovers_at_incident_moment(isolated_store):
    """决定性反例：journal seq 37 原文 × 事故时刻（归档仅 2 条）。

    旧修复（容量基授权）在此形态下永久失败 —— 压制使冻结 digest 永远没有
    匹配回执，任何 cap 无效。新契约：压制链 marker 与冻结 digest 匹配 +
    horizon 已过 + audit inactive → 授权，不要求归档满容量。
    """
    freeze, audit = _build_incident_shape()
    assert _history_len(audit) < store.RESUME_RECEIPT_HISTORY_CAP, (
        "precondition: the incident-time archive was NOT full (production had 2)"
    )

    errors = _worker_availability_resume_receipt_errors(
        freeze, audit, now=INCIDENT_MOMENT
    )
    assert errors == [], errors


def test_incident_shape_horizon_not_elapsed_still_fails_closed(isolated_store):
    """同一形态、horizon 未过（observed_at+120s 之前）仍 fail-closed。"""
    freeze, audit = _build_incident_shape()
    horizon = datetime.fromisoformat(freeze["observed_at"]) + timedelta(seconds=120)
    errors = _worker_availability_resume_receipt_errors(
        freeze, audit, now=horizon - timedelta(seconds=1)
    )
    assert errors
    assert "global_pause_resume_receipt_no_archived_match" in errors


def test_incident_shape_survives_archive_churn_via_marker_chain(isolated_store):
    """风暴继续把归档填满后仍授权：压制 marker 随回执投影进入归档链。

    两段断言：(a) 归档填到真实 cap 64 —— 旧代码靠容量道也放行，新代码必须
    靠归档回执里的 marker 链；(b) 把 cap monkeypatch 到远大于归档深度 ——
    容量道永远不可能满足，授权只能来自压制链本身。
    """
    freeze, audit = _build_incident_shape()

    # (a) fill the retained archive to the real cap.
    start = store._parse_time(audit["auto_resume_at"]) or INCIDENT_MOMENT
    moment = _sawtooth_until_full(
        start + timedelta(seconds=2), cap=store.RESUME_RECEIPT_HISTORY_CAP
    )
    audit_full = store.load_llm_pause()
    assert _history_len(audit_full) == store.RESUME_RECEIPT_HISTORY_CAP
    assert audit_full.get("last_suppressed_evidence_digest") != freeze["evidence_digest"], (
        "precondition: the current record is a fresh storm record — the marker "
        "survives only inside the archived receipt projection"
    )
    errors = _worker_availability_resume_receipt_errors(
        freeze, audit_full, now=moment + timedelta(hours=1)
    )
    assert errors == [], errors

    # (b) same storm replayed under an effectively unbounded cap: the archive
    # never reaches "full", so only the marker chain can authorize.
    monkeypatch_cap = 10**6
    real_cap = store.RESUME_RECEIPT_HISTORY_CAP
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(store, "RESUME_RECEIPT_HISTORY_CAP", monkeypatch_cap)
        freeze2, audit2 = _build_incident_shape()
        start2 = store._parse_time(audit2["auto_resume_at"]) or INCIDENT_MOMENT
        # fill past the REAL cap (66 > 64) while the patched store cap keeps
        # every receipt retained — the capacity lane can never fire.
        moment2 = _sawtooth_until_full(start2 + timedelta(seconds=2), cap=real_cap + 2)
        audit_partial = store.load_llm_pause()
        assert _history_len(audit_partial) < monkeypatch_cap
        errors2 = _worker_availability_resume_receipt_errors(
            freeze2, audit_partial, now=moment2 + timedelta(hours=1)
        )
    assert errors2 == [], errors2


def test_marker_receipt_evicted_from_bounded_archive_fails_closed(isolated_store):
    """压制链受有界归档约束：携带 marker 的回执被 FIFO 挤出后，冻结 digest
    在链上无迹可查 → fail-closed（结构绑定意味着链上无据即拒，永不凭
    「归档满」放行）。"""
    freeze, audit = _build_incident_shape()
    # The suppressed record's receipt (carrying the journal digest's marker)
    # is among the oldest archived entries (position 3: two pre-wedge + the
    # suppressed record).  Drive enough cycles that the retained FIFO window
    # (cap 64) slides past it: total archives 3 + cycles >= cap + 3.
    start = store._parse_time(audit["auto_resume_at"]) or INCIDENT_MOMENT
    moment = _sawtooth_cycles(
        start + timedelta(seconds=2), store.RESUME_RECEIPT_HISTORY_CAP + 3
    )
    audit_full = store.load_llm_pause()
    assert _history_len(audit_full) == store.RESUME_RECEIPT_HISTORY_CAP
    chain_records = [audit_full, *(
        entry
        for entry in (audit_full.get("resume_receipt_history") or [])
        if isinstance(entry, dict)
    )]
    assert all(
        entry.get("last_suppressed_evidence_digest") != freeze["evidence_digest"]
        for entry in chain_records
    ), "precondition: the marker-bearing receipt was evicted from the chain"

    errors = _worker_availability_resume_receipt_errors(
        freeze, audit_full, now=moment + timedelta(hours=1)
    )
    assert errors
    assert "global_pause_resume_receipt_no_archived_match" in errors


# ---------------------------------------------------------------------------
# F-M1：容量基授权删除 —— 伪造 digest × 满容量归档仍拒
# ---------------------------------------------------------------------------


def _fill_archive_full(start: datetime) -> datetime:
    return _sawtooth_until_full(
        start, cap=store.RESUME_RECEIPT_HISTORY_CAP
    )


def test_forged_digest_never_authorizes_even_with_full_archive(isolated_store):
    """EXP-1 反例：伪造 digest（从未持久化）× 满 64 条归档 → 必须拒绝。"""
    moment = _fill_archive_full(BASE)
    audit = store.load_llm_pause()
    assert audit["active"] is False
    assert _history_len(audit) == store.RESUME_RECEIPT_HISTORY_CAP

    forged = dict(audit)
    forged["evidence_digest"] = hashlib.sha256(b"never-existed-digest").hexdigest()
    forged["category"] = SERVICE_UNAVAILABLE
    forged["requires_manual_resume"] = False
    forged["auto_resume_at"] = (BASE + timedelta(seconds=1)).isoformat()

    errors = _worker_availability_resume_receipt_errors(
        forged, audit, now=moment + timedelta(hours=1)
    )
    assert errors, "a forged digest must never authorize via any capacity lane"
    assert "global_pause_resume_receipt_no_archived_match" in errors


def test_superseded_active_pause_without_marker_fails_closed(isolated_store):
    """EXP-2/repro 形态：P1 被更高优先级 quota 覆写（非压制分支）→ P1 既无
    回执也无压制 marker —— 满容量归档下仍 fail-closed（结构性绑定）。"""
    p1 = store.persist_llm_pause(
        _blip("HTTP 529 P1 superseded by quota"), now=BASE
    )
    deferred = dict(p1)
    quota_body = (
        "Request rejected (429) · [1308][已达到 5 小时的使用上限。"
        "您的限额将在 2026-10-09T05:05:00+00:00 重置。]"
    )
    quota = classify_llm_availability([quota_body], statuses=[429])
    assert quota is not None and quota.category == QUOTA_429
    store.persist_llm_pause(quota, now=BASE + timedelta(seconds=1))
    reset = store._parse_provider_reset_time("2026-10-09T05:05:00+00:00")
    assert store.active_llm_pause(now=reset + timedelta(seconds=1)) is None

    moment = _fill_archive_full(reset + timedelta(seconds=2))
    audit = store.load_llm_pause()
    assert _history_len(audit) == store.RESUME_RECEIPT_HISTORY_CAP
    assert not any(
        entry.get("evidence_digest") == p1["evidence_digest"]
        for entry in (audit.get("resume_receipt_history") or [])
    ), "precondition: P1's receipt never existed"

    errors = _worker_availability_resume_receipt_errors(
        deferred, audit, now=moment + timedelta(hours=1)
    )
    assert errors
    assert "global_pause_resume_receipt_no_archived_match" in errors


# ---------------------------------------------------------------------------
# F-M2：压制/快照授权必须发独立审计事件
# ---------------------------------------------------------------------------


def test_suppression_authorization_returns_descriptor(isolated_store):
    """校验器必须暴露授权描述（形态/冻结 digest/归档深度/horizon）。"""
    freeze, audit = _build_incident_shape()
    errors, authorization = (
        tool_planning_worker_durable._worker_availability_resume_validation(
            freeze, audit, now=INCIDENT_MOMENT
        )
    )
    assert errors == [], errors
    assert isinstance(authorization, dict)
    assert authorization["authorization_shape"] == "suppressed_evidence_chain"
    assert authorization["evidence_digest"] == freeze["evidence_digest"]
    assert authorization["archive_depth"] == _history_len(audit)
    assert store._parse_time(authorization["horizon"]) == (
        datetime.fromisoformat(freeze["observed_at"]) + timedelta(seconds=120)
    )


def test_receipt_lanes_do_not_emit_snapshot_authorization(isolated_store):
    """当前记录 / 归档回执两道授权返回 authorization=None（不发事件）。"""
    pause = store.persist_llm_pause(_blip("HTTP 529 receipt lane D1"), now=BASE)
    deferred = dict(pause)
    _reconcile(BASE + timedelta(seconds=30))
    audit = store.load_llm_pause()
    errors, authorization = (
        tool_planning_worker_durable._worker_availability_resume_validation(
            deferred, audit, now=BASE + timedelta(minutes=5)
        )
    )
    assert errors == [], errors
    assert authorization is None


def test_snapshot_authorization_event_emitted_with_required_fields(
    isolated_store, monkeypatch
):
    """授权事件：pipeline.worker_resume_snapshot_authorized + 必备字段。"""
    events = []
    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            {
                "type": event_type,
                "severity": severity,
                "message": message,
                "data": data or {},
            }
        ),
    )
    freeze, audit = _build_incident_shape()
    authorization = (
        tool_planning_worker_durable._worker_availability_resume_validation(
            freeze, audit, now=INCIDENT_MOMENT
        )[1]
    )
    assert authorization is not None
    tool_planning_worker_durable._emit_worker_resume_snapshot_authorized(
        authorization, effect_id="worker:generation:532:workflow-v1:cycle-4:60a655915bcaf930"
    )
    assert events, "the snapshot authorization must emit a system event"
    event = events[-1]
    assert event["type"] == "pipeline.worker_resume_snapshot_authorized"
    assert event["data"]["effect_id"] == (
        "worker:generation:532:workflow-v1:cycle-4:60a655915bcaf930"
    )
    assert event["data"]["evidence_digest"] == freeze["evidence_digest"]
    assert event["data"]["authorization_shape"] == "suppressed_evidence_chain"
    assert event["data"]["archive_depth"] == _history_len(audit)
    assert event["data"]["horizon"] == authorization["horizon"]


def test_phases_receipt_gate_emits_event_on_snapshot_lane(monkeypatch):
    """phases 校验楼口：压制授权时经 effect_id 发射事件；回执授权不发。"""
    emitted = []

    def fake_emit(authorization, *, effect_id=None):
        emitted.append((authorization, effect_id))

    monkeypatch.setattr(
        tool_planning_worker_durable,
        "_emit_worker_resume_snapshot_authorized",
        fake_emit,
    )
    descriptor = {
        "authorization_shape": "suppressed_evidence_chain",
        "evidence_digest": "d" * 64,
        "category": SERVICE_UNAVAILABLE,
        "horizon": "2026-10-08T21:08:56.041982+00:00",
        "archive_depth": 2,
        "matched_record": "current",
    }
    monkeypatch.setattr(
        tool_planning_worker_durable,
        "_worker_availability_resume_validation",
        lambda deferred, audit, now=None: ([], dict(descriptor)),
    )
    gate = tool_planning_worker_phases._worker_resume_receipt_gate
    state = {"effect_id": "worker:generation:532:workflow-v1:cycle-4:60a655915bcaf930"}

    errors = gate(state, {"evidence_digest": "d" * 64}, {"active": False})
    assert errors == []
    assert emitted and emitted[-1][1] == state["effect_id"]

    # receipt lane: authorization None -> no emission.
    monkeypatch.setattr(
        tool_planning_worker_durable,
        "_worker_availability_resume_validation",
        lambda deferred, audit, now=None: ([], None),
    )
    emitted.clear()
    assert gate(state, {}, {}) == []
    assert emitted == []

    # failure lane: errors flow through unchanged, no emission.
    monkeypatch.setattr(
        tool_planning_worker_durable,
        "_worker_availability_resume_validation",
        lambda deferred, audit, now=None: (["global_pause_resume_receipt_no_archived_match"], None),
    )
    assert gate(state, {}, {}) == ["global_pause_resume_receipt_no_archived_match"]
    assert emitted == []


# ---------------------------------------------------------------------------
# F-L：horizon 的 quota 解析与 store 权威对齐
# ---------------------------------------------------------------------------


def test_quota_horizon_uses_store_authoritative_host_local_parse():
    naive_local = "2026-10-09 20:00:00"
    authoritative = store._parse_provider_reset_time(naive_local)
    assert authoritative is not None
    horizon = store.system_resume_horizon(
        {
            "category": QUOTA_429,
            "requires_manual_resume": False,
            "observed_at": BASE.isoformat(),
            "provider_reset_at": naive_local,
        }
    )
    assert horizon == authoritative, (
        "the horizon must parse provider_reset_at with the store's "
        "authoritative host-local rule, not naive->UTC"
    )


def test_quota_horizon_matches_real_frozen_projection():
    """经 classify→freeze 的真实投影（naive 本地重置串）与权威解析一致。"""
    reset_local = "2026-10-09 23:30:00"
    body = (
        "Request rejected (429) · [1308][已达到 5 小时的使用上限。"
        f"您的限额将在 {reset_local} 重置。]"
    )
    issue = classify_llm_availability([body], statuses=[429])
    assert issue is not None and issue.category == QUOTA_429
    frozen = build_llm_pause_state(issue, observed_at=BASE.isoformat())
    horizon = store.system_resume_horizon(frozen)
    assert horizon == store._parse_provider_reset_time(reset_local)


# ---------------------------------------------------------------------------
# F-M3：慢道 running 栅栏 —— 裸 stop 不复活
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 1000.0):
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _RecordingUI:
    def __init__(self):
        self.history = []

    def log_history(self, msg, status="info"):
        self.history.append({"msg": msg, "status": status})


@pytest.fixture
def supervisor_env(monkeypatch):
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_INITIAL_BACKOFF_SEC", "0.001")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BACKOFF_SEC", "0.004")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BURST", "2")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_WINDOW_SEC", "30")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_STABLE_RUN_SEC", "999999")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC", "5")

    clock = _FakeClock()
    monkeypatch.setattr(state_module, "time", clock)

    # Gated slow-lane sleep: blocks until the test opens the gate (the
    # red-team probe technique — the supervisor truly parks).
    gate = asyncio.Event()

    async def fake_sleep(seconds: float) -> None:
        if seconds >= 1.0:
            await gate.wait()
        else:
            await asyncio.sleep(0)
        clock.advance(seconds)

    monkeypatch.setattr(state_module, "_restart_backoff_sleep", fake_sleep)

    events = []
    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            {"type": event_type, "severity": severity, "message": message, "data": data or {}}
        ),
    )
    ui = _RecordingUI()
    import tool_helpers

    monkeypatch.setattr(tool_helpers, "_get_ui", lambda: ui)
    return {"clock": clock, "events": events, "ui": ui, "gate": gate}


@pytest.fixture(autouse=True)
def _quiesce_app_state():
    from server.state import app_state

    app_state.set_running(False)
    app_state._last_orchestrator_crash = None
    yield
    app_state.set_running(False)
    app_state._last_orchestrator_crash = None


def _restart_statuses(env):
    return [
        e["data"].get("status")
        for e in env["events"]
        if e["type"] == "pipeline.orchestrator_auto_restart"
    ]


async def _drive_realistic(env, outcomes):
    """Factory bodies mirror the real loop-finally: they clear ``running``."""
    from server.state import app_state, run_evolution_task

    calls = []
    owner_id = app_state.begin_runtime_owner()
    assert owner_id is not None

    def factory():
        calls.append(len(calls) + 1)

        async def body():
            app_state.set_running(False)  # loop-finally semantics on exit
            if len(calls) <= len(outcomes):
                return outcomes[len(calls) - 1]
            return 0.0

        return body()

    task = asyncio.create_task(
        run_evolution_task(factory(), owner_id=owner_id, restart_factory=factory)
    )
    app_state.set_task(task, owner_id=owner_id)
    return task, calls, owner_id


def test_bare_stop_during_slow_lane_park_never_revives(supervisor_env):
    """F-M3 反例：裸 stop_running（无 shutdown/cancel）停车后不得静默复活。"""

    async def scenario():
        from server.state import app_state

        task, calls, owner_id = await _drive_realistic(
            supervisor_env, [-1.0, -1.0, -1.0]
        )
        # Wait until the supervisor parks in the slow lane.
        for _ in range(2000):
            await asyncio.sleep(0.005)
            if "restart_rate_limited" in _restart_statuses(supervisor_env):
                break
        assert "restart_rate_limited" in _restart_statuses(supervisor_env)
        before = len(calls)

        # Bare stop: mark stopped, keep the task + owner alive (no shutdown,
        # no cancel) — the boundary hole from the review.
        returned = app_state.stop_running(owner_id=owner_id)
        assert returned is task and not task.done()

        # Release the gated slow-lane sleep and give the loop a turn.
        supervisor_env["gate"].set()
        for _ in range(50):
            await asyncio.sleep(0.005)
        supervisor_env["gate"].clear()

        assert len(calls) == before, "a bare stop must not silently revive"
        assert "stopped_no_restart" in _restart_statuses(supervisor_env)
        # The supervisor exited: the wrapper finished, so a fresh Start is
        # possible again (the 409 already_owned window is closed).
        for _ in range(200):
            if task.done():
                break
            await asyncio.sleep(0.005)
        assert task.done()
        assert await task == -1.0
        assert app_state.begin_runtime_owner() is not None

    asyncio.run(scenario())


def test_slow_lane_park_rearms_running_intent(supervisor_env):
    """停车期间监督器 re-arm ``running``：栅栏的可观测前提。"""

    async def scenario():
        from server.state import app_state

        task, calls, _owner = await _drive_realistic(
            supervisor_env, [-1.0, -1.0, -1.0]
        )
        for _ in range(2000):
            await asyncio.sleep(0.005)
            if "restart_rate_limited" in _restart_statuses(supervisor_env):
                break
        assert "restart_rate_limited" in _restart_statuses(supervisor_env)
        # Parked: the supervisor owns the runtime intent while it waits.
        assert app_state.to_dict()["running"] is True
        task.cancel()
        try:
            await task
        except BaseException:
            pass

    asyncio.run(scenario())


def test_slow_lane_still_recovers_with_realistic_crash_cleanup(supervisor_env):
    """栅栏不得误伤正常慢道恢复：crash 清 running → re-arm → 下轮恢复。"""

    async def scenario():
        from server.state import app_state

        supervisor_env["gate"].set()  # let the parked slow-lane sleeps pass
        task, calls, _owner = await _drive_realistic(
            supervisor_env, [-1.0, -1.0, -1.0, 0.0]
        )
        result = await task
        assert result == 0.0
        assert calls == [1, 2, 3, 4]
        assert "slow_retry_scheduled" in _restart_statuses(supervisor_env)
        assert app_state.to_dict()["running"] is False

    asyncio.run(scenario())


def test_rate_limited_alarm_documents_stop_then_start_contract(supervisor_env, caplog):
    """告警文案必须写明 Stop-then-Start 契约（停车期 /start 409 already_owned）。"""
    caplog.set_level(logging.ERROR, logger="pok.orchestrator")

    async def scenario():
        supervisor_env["gate"].set()
        task, calls, _owner = await _drive_realistic(
            supervisor_env, [-1.0, -1.0, -1.0, 0.0]
        )
        await task

    asyncio.run(scenario())
    messages = [entry.getMessage() for entry in caplog.records]
    contract = [m for m in messages if "Stop" in m and "Start" in m]
    assert contract, "the rate-limited alarm must document the Stop-then-Start contract"
