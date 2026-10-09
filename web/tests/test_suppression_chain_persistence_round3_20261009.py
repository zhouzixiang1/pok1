"""2026-10-09 第三轮对抗审计残留缺口（R1-R4）的契约测试.

上一笔修复（a22d1eea，压制链授权）经两轮对抗审计后仍残留四项同族缺口，
全部被复核员实测复现。本文件钉住四项修订的契约：

* **R1（marker 单槽 last-write-wins）**：同一 ACTIVE ``service_unavailable``
  记录的冷却窗口内，先冻结的 Worker digest X1 写入
  ``last_suppressed_evidence_digest`` 后被后冻结的 X2 覆盖，X1 在任何
  durable 面消失，持有 X1 的 deferred 效应永久 fail-closed。修订：压制
  marker 改为**有界历史列表** ``last_suppressed_records``（每项
  ``{"category", "evidence_digest"}``，FIFO，上限 34（覆盖 AIMD_MAX_LIMIT=32 并发上界），按 digest 去重），
  标量 ``last_suppressed_*`` 保留为"最新一条"的向后兼容投影；归档回执
  投影（``_RESUME_RECEIPT_PROJECTION_FIELDS``）同样携带列表；压制链查询
  扫描标量 + 列表。不变式：**只有 store 真实压制过的 digest 才进列表**
  （伪造 digest 在任何列表深度下仍 fail-closed）。
* **R2（带 marker 的 ACTIVE 记录被更高优先级 pause 覆写时 marker 蒸发）**：
  1302 风暴 → 1308 quota 的现实升级序列中，替换分支把 ACTIVE 记录连同
  marker 整体丢弃且不归档（``_resume_receipt_projection`` 拒绝 ACTIVE
  记录）。修订：替换 ACTIVE 记录时，若其携带压制 marker（标量或列表），
  把 marker **摊平进一条 ``suppressed_marker_eviction`` 归档投影**追加进
  ``resume_receipt_history``（选择摊平而非前向携带的论证见
  ``llm_availability_store`` 内注释：前向携带会让 quota 记录的标量字段
  谎报"最新压制"归属、并把 marker 生命周期耦合到未来记录链而非有界
  归档槽）。无 marker 的 ACTIVE 记录被覆写不产生投影（F-M1 既有契约
  不变：被顶替记录自身的 digest 仍 fail-closed）。
* **R3（授权事件 data.category 被 event_bus 保留键覆盖）**：
  ``log_system_event`` 会 ``pop("category")``（``emit`` 的首位形参名就是
  ``category``），``event_bus.emit`` 再回填事件类型 → 落盘
  ``data.category`` 恒等于事件名，pause 类别丢失。修订：发射点
  （``_emit_worker_resume_snapshot_authorized``）改键名
  ``pause_category``；落盘断言走**真实 system_log→event_bus→events.jsonl**
  链路（仅隔离 RESULTS_DIR，不 monkeypatch 发射/落盘函数）。
* **R4（慢道 0 值忙自旋）**：``_env_float_in_range`` 下界 0.0 使
  ``POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC=0`` 被接受 → 停车道
  ``await sleep(0)`` 以 ~10 万次/秒协作自旋（实测 0.3s 内 38658 次
  零长睡眠），注释"never spins"为假。修订：下界钳到 **1.0**（与
  ``_restart_max_backoff_seconds`` / ``_restart_window_seconds`` 的既有
  下界约定一致：越界回退默认值 1800.0），注释同步修正。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json

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
import tool_planning_worker  # noqa: F401  (parent-first import order)
import tool_planning_worker_durable as durable


BASE = datetime(2026, 10, 9, 5, 0, tzinfo=timezone.utc)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.delenv(store.RESUME_ENV, raising=False)
    return tmp_path


def _service_issue(text: str):
    issue = classify_llm_availability([text], statuses=[529])
    assert issue is not None and issue.category == SERVICE_UNAVAILABLE
    return issue


def _quota_1308_issue(reset_iso: str = "2026-10-09T10:00:00+00:00"):
    body = (
        "Request rejected (429) · [1308][已达到 5 小时的使用上限。"
        f"您的限额将在 {reset_iso} 重置。]"
    )
    issue = classify_llm_availability([body], statuses=[429])
    assert issue is not None and issue.category == QUOTA_429
    return issue


def _freeze(text: str, role: str, observed_at: datetime):
    return build_llm_pause_state(
        _service_issue(text), role=role, observed_at=observed_at.isoformat()
    )


def _errors(frozen: dict, audit: dict, now: datetime):
    return durable._worker_availability_resume_receipt_errors(frozen, audit, now=now)


def _reconcile_inactive(now: datetime) -> dict:
    record = store.load_llm_pause()
    due = store._parse_time((record or {}).get("auto_resume_at")) if record else None
    if due is None or now > due:
        assert store.active_llm_pause(now=now) is None
    return store.load_llm_pause()


def _marker_digests(entry: dict):
    raw = entry.get("last_suppressed_records")
    if not isinstance(raw, list):
        return []
    return [
        str(item.get("evidence_digest") or "")
        for item in raw
        if isinstance(item, dict)
    ]


# ---------------------------------------------------------------------------
# R1: 同一冷却窗口内双冻结 —— X1 与 X2 都必须在链上存活并授权
# ---------------------------------------------------------------------------


def _dual_freeze_shape():
    """风暴 ACTIVE 记录 + 同窗口两个不同 digest 的异常路径冻结（SHAPE-A）。"""
    store.persist_llm_pause(_service_issue("HTTP 529 storm record A0"), now=BASE)
    x1 = _freeze("HTTP 529 worker W1 stream failed reqid=1111", "WORKER w1", BASE + timedelta(seconds=30))
    store.persist_llm_pause(x1, now=BASE + timedelta(seconds=30))
    x2 = _freeze("HTTP 529 worker W2 stream failed reqid=2222", "WORKER w2", BASE + timedelta(seconds=60))
    persisted = store.persist_llm_pause(x2, now=BASE + timedelta(seconds=60))
    return x1, x2, persisted


def test_same_window_dual_freeze_keeps_both_markers(isolated_store):
    """R1 核心：X2 覆盖标量槽后，X1 必须仍留在有界 marker 列表里。"""
    x1, x2, persisted = _dual_freeze_shape()

    # 标量槽保持"最新一条"语义（向后兼容投影），列表同时持有两者。
    assert persisted.get("last_suppressed_evidence_digest") == x2["evidence_digest"]
    digests = _marker_digests(persisted)
    assert x1["evidence_digest"] in digests
    assert x2["evidence_digest"] in digests


def test_dual_freeze_both_authorize_on_current_record(isolated_store):
    """记录冷却转为 inactive 后（marker 仍在当前记录上）：X1 与 X2 都授权。"""
    x1, x2, persisted = _dual_freeze_shape()
    due = store._parse_time(persisted["auto_resume_at"])
    audit = _reconcile_inactive(due + timedelta(seconds=1))
    assert audit["active"] is False

    later = due + timedelta(minutes=5)
    assert _errors(x1, audit, later) == []
    assert _errors(x2, audit, later) == []


def test_dual_freeze_both_authorize_through_archived_receipt(isolated_store):
    """记录恢复后被新风暴记录替换：归档回执投影携带列表，X1/X2 仍都授权。"""
    x1, x2, persisted = _dual_freeze_shape()
    due = store._parse_time(persisted["auto_resume_at"])
    audit = _reconcile_inactive(due + timedelta(seconds=1))
    assert audit["active"] is False

    # 新记录替换 → 压制记录的回执投影（含 marker 列表）进归档。
    repl = store.persist_llm_pause(
        _service_issue("HTTP 529 fresh later storm record Z"),
        now=due + timedelta(seconds=2),
    )
    ondisk = store.load_llm_pause()
    history = ondisk.get("resume_receipt_history") or []
    marker_entries = [
        e for e in history if isinstance(e, dict) and _marker_digests(e)
    ]
    assert marker_entries, "the archived receipt projection must carry the marker list"
    archived_digests = {
        d for e in marker_entries for d in _marker_digests(e)
    }
    assert x1["evidence_digest"] in archived_digests
    assert x2["evidence_digest"] in archived_digests

    due_z = store._parse_time(repl["auto_resume_at"])
    audit_z = _reconcile_inactive(due_z + timedelta(seconds=1))
    later = due_z + timedelta(minutes=5)
    errors1, auth1 = durable._worker_availability_resume_validation(x1, audit_z, now=later)
    errors2, auth2 = durable._worker_availability_resume_validation(x2, audit_z, now=later)
    assert errors1 == [] and errors2 == []
    assert auth1 is not None and auth1["authorization_shape"] == "suppressed_evidence_chain"
    assert auth2 is not None


def test_marker_history_is_bounded_fifo_cap_34(isolated_store):
    """列表有界：40 个不同 digest 冻结后只留最新 34 个，最旧的 fail-closed。"""
    store.persist_llm_pause(_service_issue("HTTP 529 storm record B0"), now=BASE)
    freezes = []
    moment = BASE
    for index in range(40):
        moment = moment + timedelta(seconds=10)
        frozen = _freeze(
            f"HTTP 529 worker freeze burst reqid=b{index:02d}", f"WORKER b{index}", moment
        )
        store.persist_llm_pause(frozen, now=moment)
        freezes.append(frozen)

    record = store.load_llm_pause()
    digests = _marker_digests(record)
    assert len(digests) == 34, "the marker history must be bounded at 34"
    assert digests == [f["evidence_digest"] for f in freezes[6:]], (
        "FIFO keeps the NEWEST thirty-four markers"
    )

    # 记录冷却 → 替换 → 被 FIFO 挤出列表的最旧 marker 在链上无迹 → fail-closed。
    due = store._parse_time(record["auto_resume_at"])
    _reconcile_inactive(due + timedelta(seconds=1))
    repl = store.persist_llm_pause(
        _service_issue("HTTP 529 post-burst record B1"), now=due + timedelta(seconds=2)
    )
    due_b1 = store._parse_time(repl["auto_resume_at"])
    audit = _reconcile_inactive(due_b1 + timedelta(seconds=1))
    later = due_b1 + timedelta(minutes=5)

    oldest = freezes[0]
    assert _errors(oldest, audit, later), (
        "an evicted marker digest has no durable trace and must fail closed"
    )
    newest = freezes[-1]
    assert _errors(newest, audit, later) == []
    survivor = freezes[10]  # 第 11 个冻结（0-based 10）仍在 34 窗口内（40 冻结挤掉最旧 6 个）
    assert _errors(survivor, audit, later) == []


def test_forged_digest_fails_closed_with_marker_list_present(isolated_store):
    """真伪不变：从未被 store 压制过的 digest 即使在满列表面前也拒绝。"""
    x1, x2, persisted = _dual_freeze_shape()
    forged = dict(x1)
    forged["evidence_digest"] = hashlib.sha256(b"round3-forged-digest").hexdigest()
    due = store._parse_time(persisted["auto_resume_at"])
    _reconcile_inactive(due + timedelta(seconds=1))
    # 替换出一条新记录使归档非空（no_archived_match 语义需要归档被扫描过）。
    repl = store.persist_llm_pause(
        _service_issue("HTTP 529 later storm record Z1"), now=due + timedelta(seconds=2)
    )
    due_z = store._parse_time(repl["auto_resume_at"])
    audit = _reconcile_inactive(due_z + timedelta(seconds=1))
    errors, auth = durable._worker_availability_resume_validation(
        forged, audit, now=due_z + timedelta(minutes=5)
    )
    assert errors
    assert "global_pause_resume_receipt_no_archived_match" in errors
    assert auth is None


def test_marker_list_dedupes_repeated_digest(isolated_store):
    """同一 digest 重复压制不占多个槽（去重后移动到最新位置）。"""
    store.persist_llm_pause(_service_issue("HTTP 529 storm record C0"), now=BASE)
    frozen = _freeze("HTTP 529 worker repeat freeze reqid=c1", "WORKER c1", BASE + timedelta(seconds=10))
    store.persist_llm_pause(frozen, now=BASE + timedelta(seconds=10))
    other = _freeze("HTTP 529 worker other freeze reqid=c2", "WORKER c2", BASE + timedelta(seconds=20))
    store.persist_llm_pause(other, now=BASE + timedelta(seconds=20))
    store.persist_llm_pause(frozen, now=BASE + timedelta(seconds=30))

    record = store.load_llm_pause()
    digests = _marker_digests(record)
    assert digests.count(frozen["evidence_digest"]) == 1, "one slot per digest"
    assert digests[-1] == frozen["evidence_digest"], "re-suppression refreshes recency"
    assert other["evidence_digest"] in digests


# ---------------------------------------------------------------------------
# R2: 带 marker 的 ACTIVE 记录被更高优先级 pause 覆写 —— marker 摊平进归档
# ---------------------------------------------------------------------------


def _quota_overwrite_shape():
    """1302 风暴 → Worker 冻结 XB → 1308 quota 在记录仍 ACTIVE 时覆写。"""
    storm = store.persist_llm_pause(
        _service_issue("HTTP 529 storm record A0"), now=BASE
    )
    assert storm["active"] is True
    freeze_x = _freeze(
        "HTTP 529 worker X stream failed reqid=xxxx",
        "WORKER X",
        BASE + timedelta(seconds=1),
    )
    persisted = store.persist_llm_pause(freeze_x, now=BASE + timedelta(seconds=1))
    assert persisted.get("last_suppressed_evidence_digest") == freeze_x["evidence_digest"]

    quota = store.persist_llm_pause(
        _quota_1308_issue("2026-10-09T05:30:00+00:00"), now=BASE + timedelta(seconds=2)
    )
    assert quota["category"] == QUOTA_429
    return freeze_x, quota


def test_active_marker_overwritten_by_quota_flattens_into_archive(isolated_store):
    """R2 核心：覆写后新 state 无 marker 字段，归档多一条压制蒸发投影。"""
    freeze_x, quota = _quota_overwrite_shape()

    ondisk = store.load_llm_pause()
    # 新 quota 记录自身不携带压制 marker（摊平归档，而非前向携带）。
    assert "last_suppressed_evidence_digest" not in ondisk
    assert "last_suppressed_records" not in ondisk
    # 归档里恰好一条 suppressed_marker_eviction 投影携带 XB。
    history = ondisk.get("resume_receipt_history") or []
    evictions = [
        e
        for e in history
        if isinstance(e, dict)
        and e.get("archive_kind") == "suppressed_marker_eviction"
    ]
    assert len(evictions) == 1
    eviction = evictions[0]
    assert eviction.get("last_suppressed_evidence_digest") == freeze_x["evidence_digest"]
    assert eviction.get("last_suppressed_category") == SERVICE_UNAVAILABLE
    assert freeze_x["evidence_digest"] in _marker_digests(eviction)

    # quota 重置到点 → 记录 inactive → 链仍可证，WB(XB) 授权。
    after_reset = datetime(2026, 10, 9, 5, 30, 1, tzinfo=timezone.utc)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()
    assert audit["active"] is False
    errors, auth = durable._worker_availability_resume_validation(
        freeze_x, audit, now=after_reset + timedelta(minutes=5)
    )
    assert errors == [], errors
    assert auth is not None
    assert auth["authorization_shape"] == "suppressed_evidence_chain"
    assert auth["matched_record"] == "archived"


def test_plain_superseded_active_record_adds_no_eviction_entry(isolated_store):
    """无 marker 的 ACTIVE 记录被 quota 顶替：不产生投影，旧契约不变。"""
    p1 = store.persist_llm_pause(
        _service_issue("HTTP 529 P1 superseded by quota"), now=BASE
    )
    deferred = dict(p1)
    assert not p1.get("last_suppressed_evidence_digest")
    store.persist_llm_pause(
        _quota_1308_issue("2026-10-09T05:30:00+00:00"), now=BASE + timedelta(seconds=1)
    )
    ondisk = store.load_llm_pause()
    history = ondisk.get("resume_receipt_history") or []
    assert history == [], "no markers -> no eviction projection"

    after_reset = datetime(2026, 10, 9, 5, 30, 1, tzinfo=timezone.utc)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()
    errors, auth = durable._worker_availability_resume_validation(
        deferred, audit, now=after_reset + timedelta(minutes=5)
    )
    assert errors, "F-M1 contract unchanged: P1's own digest still fails closed"
    # 历史为空（无回执、无蒸发投影），拒绝走当前记录 mismatch 令牌路径。
    assert "global_pause_resume_receipt_evidence_digest_mismatch" in errors
    assert auth is None


def test_eviction_entry_survives_history_carry_forward(isolated_store):
    """蒸发投影必须随 resume_receipt_history 一起被向前携带（过滤器放行）。"""
    freeze_x, quota = _quota_overwrite_shape()

    # 后续风暴循环：每条记录 persist 后冷却 —— carry-forward 不得丢掉投影。
    moment = datetime(2026, 10, 9, 5, 30, 2, tzinfo=timezone.utc)
    for index in range(3):
        store.persist_llm_pause(
            _service_issue(f"HTTP 529 post-quota storm blip {index}"), now=moment
        )
        record = store.load_llm_pause()
        due = store._parse_time(record["auto_resume_at"])
        moment = due + timedelta(seconds=1)
        assert store.active_llm_pause(now=moment) is None

    audit = store.load_llm_pause()
    history = audit.get("resume_receipt_history") or []
    evictions = [
        e for e in history if e.get("archive_kind") == "suppressed_marker_eviction"
    ]
    assert evictions, "the eviction projection must survive carry-forward churn"
    errors = _errors(freeze_x, audit, moment + timedelta(minutes=5))
    assert errors == [], errors


def test_eviction_marker_list_carried_into_new_record_markers(isolated_store):
    """多 marker 列表（R1×R2 复合）：双冻结后 quota 覆写，X1/X2 都可证。"""
    store.persist_llm_pause(_service_issue("HTTP 529 storm record D0"), now=BASE)
    x1 = _freeze("HTTP 529 worker d1 reqid=d1", "WORKER d1", BASE + timedelta(seconds=1))
    store.persist_llm_pause(x1, now=BASE + timedelta(seconds=1))
    x2 = _freeze("HTTP 529 worker d2 reqid=d2", "WORKER d2", BASE + timedelta(seconds=2))
    store.persist_llm_pause(x2, now=BASE + timedelta(seconds=2))
    store.persist_llm_pause(
        _quota_1308_issue("2026-10-09T05:30:00+00:00"), now=BASE + timedelta(seconds=3)
    )

    after_reset = datetime(2026, 10, 9, 5, 30, 1, tzinfo=timezone.utc)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()
    history = audit.get("resume_receipt_history") or []
    evictions = [
        e for e in history if e.get("archive_kind") == "suppressed_marker_eviction"
    ]
    assert len(evictions) == 1
    carried = set(_marker_digests(evictions[0]))
    assert carried == {x1["evidence_digest"], x2["evidence_digest"]}

    later = after_reset + timedelta(minutes=5)
    assert _errors(x1, audit, later) == []
    assert _errors(x2, audit, later) == []


# ---------------------------------------------------------------------------
# R3: 授权事件落盘 —— pause_category 键穿过真实 event_bus
# ---------------------------------------------------------------------------


def test_authorized_event_persists_pause_category_via_real_event_bus(
    isolated_store, monkeypatch
):
    """不 monkeypatch 发射/落盘函数：真实 system_log → event_bus → events.jsonl。

    event_bus.emit 的首位形参名就是 ``category``（事件类型）且无条件回填
    ``data["category"]``，``log_system_event`` 因此 pop 掉调用方 data 里的
    ``category`` —— 落盘后 pause 类别必须改走 ``pause_category`` 键。
    """
    import event_bus

    # 仅重置 events.jsonl 路径缓存（RESULTS_DIR 隔离），发射链路保持真实。
    monkeypatch.setattr(event_bus, "EVENTS_FILE", None)

    authorization = {
        "authorization_shape": "suppressed_evidence_chain",
        "evidence_digest": "d" * 64,
        "category": SERVICE_UNAVAILABLE,
        "horizon": "2026-10-09T05:08:56.041982+00:00",
        "archive_depth": 1,
        "matched_record": "archived",
    }
    durable._emit_worker_resume_snapshot_authorized(
        authorization,
        effect_id="worker:generation:532:workflow-v1:cycle-4:60a655915bcaf930",
    )

    events_path = isolated_store / "events.jsonl"
    assert events_path.exists(), "the real event_bus must persist the event"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    auth_rows = [
        r for r in rows if r.get("type") == "pipeline.worker_resume_snapshot_authorized"
    ]
    assert auth_rows, "the authorized event must be persisted"
    data = auth_rows[-1]["data"]
    assert data.get("pause_category") == SERVICE_UNAVAILABLE
    # 保留键行为的文档化断言：data.category 恒为事件类型（event_bus 回填）。
    assert data.get("category") == "pipeline.worker_resume_snapshot_authorized"
    assert data.get("evidence_digest") == "d" * 64
    assert data.get("authorization_shape") == "suppressed_evidence_chain"
    assert data.get("effect_id") == (
        "worker:generation:532:workflow-v1:cycle-4:60a655915bcaf930"
    )


# ---------------------------------------------------------------------------
# R4: 慢道 0 值忙自旋 —— 下界钳位
# ---------------------------------------------------------------------------


def test_slow_retry_zero_falls_back_to_default(monkeypatch):
    """POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC=0 越界 → 回退默认 1800.0。

    下界 1.0 与 ``_restart_max_backoff_seconds`` / ``_restart_window_seconds``
    的既有约定一致（越界回退默认）；1.0s 把停车循环钳到 ≤1 次迭代/秒，
    彻底消除 ~10 万次/秒的零长睡眠自旋，同时保留运维缩短节奏的能力。
    """
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC", "0")
    assert state_module._restart_slow_retry_seconds() == 1800.0


def test_slow_retry_sub_second_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC", "0.5")
    assert state_module._restart_slow_retry_seconds() == 1800.0


def test_slow_retry_accepts_floor_and_above(monkeypatch):
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC", "1.0")
    assert state_module._restart_slow_retry_seconds() == 1.0
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC", "2.5")
    assert state_module._restart_slow_retry_seconds() == 2.5
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC", "1800")
    assert state_module._restart_slow_retry_seconds() == 1800.0
