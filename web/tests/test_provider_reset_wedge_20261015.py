"""2026-10-15 楔死修复契约测试：trusted 1308 × ``provider_quota_reset_elapsed``.

红队探针（dwfrun-e13bccf5，medium/verified）复现的 v555 姊妹形状 —— v555 修的是
「更高优先级 ACTIVE 顶替」的蒸发投影，本文件钉住另一半形状：

1. Worker 在 **trusted 1308 配额窗口内**冻结 ACTIVE 记录自身的
   ``(category, evidence_digest)`` 作为 resume 凭据 —— claim-boundary 冻结
   （``active_llm_pause()`` 返回的记录投影，含 ``auto_resume_at``），或
   异常路径同报文冻结（``exc.pause_state()`` 入射投影 + 首次 persist）；
2. 配额到时，记录经 ``resume_source="provider_quota_reset_elapsed"``
   原地转 inactive（无新记录、无归档回执、无 marker）；
3. 三条恢复 lane 全灭：
   * receipt lane 白名单只认 ``bounded_cooldown_elapsed``；
   * equal-digest 抑制分支不写 marker（冻结 digest == 记录 digest）；
   * 记录未被更高优先级 ACTIVE 顶替 → 蒸发投影（v555 修复）不触发；
4. 结果 ``transient_pause_cooldown_receipt_missing`` →
   ``WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID`` 永久慢速重试楔死。

修订（本文件钉住）：receipt lane 的 transient 白名单对称接纳
``provider_quota_reset_elapsed`` —— 与 ``bounded_cooldown_elapsed`` 同为
``_reconcile_llm_pause`` 单点写入的系统自有 resume_source（且仅在记录自身
deadline 到点后写入），token 本身即系统恢复路径证明。其余来源
（``untrusted_quota_pause_without_reset_authorities`` / 任意串 / 手工
``operator_evidence_digest`` 走 manual 分支）保持 fail-closed。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

import pytest

import evolution_infra
from llm_availability import (
    QUOTA_429,
    SERVICE_UNAVAILABLE,
    build_llm_pause_state,
    classify_llm_availability,
)
import llm_availability_store as store
import tool_planning_worker  # noqa: F401  (parent-first import order)
import tool_planning_worker_durable as durable


BASE = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.delenv(store.RESUME_ENV, raising=False)
    return tmp_path


def _quota_1308_issue(reset_iso: str):
    body = (
        "Request rejected (429) · [1308][已达到 5 小时的使用上限。"
        f"您的限额将在 {reset_iso} 重置。]"
    )
    issue = classify_llm_availability([body], statuses=[429])
    assert issue is not None and issue.category == QUOTA_429
    return issue


def _service_issue(text: str):
    issue = classify_llm_availability([text], statuses=[529])
    assert issue is not None and issue.category == SERVICE_UNAVAILABLE
    return issue


def _validation(frozen: dict, audit: dict, now: datetime):
    return durable._worker_availability_resume_validation(frozen, audit, now=now)


def _evictions(audit: dict) -> list[dict]:
    return [
        entry
        for entry in (audit.get("resume_receipt_history") or [])
        if isinstance(entry, dict)
        and entry.get("archive_kind") == "suppressed_marker_eviction"
    ]


# ---------------------------------------------------------------------------
# (1) claim-boundary 冻结全序列回放 —— wedge 形状前提 + 修复后授权
# ---------------------------------------------------------------------------


def test_claim_boundary_quota_freeze_full_sequence_authorizes(isolated_store):
    """冻结=ACTIVE 记录自身投影 → trusted 1308 到时 → receipt lane 授权 []。"""
    reset = BASE + timedelta(minutes=30)
    record = store.persist_llm_pause(
        _quota_1308_issue(reset.isoformat()), now=BASE
    )
    assert record["active"] is True
    assert record["requires_manual_resume"] is False

    # claim-boundary 冻结：active_llm_pause() 的记录投影（含 auto_resume_at）。
    deferred = dict(
        store.active_llm_pause(now=BASE + timedelta(seconds=1))
    )
    assert deferred["evidence_digest"] == record["evidence_digest"], (
        "the claim-boundary freeze holds the ACTIVE record's own digest"
    )

    # reset 未到时（记录仍 ACTIVE）仍 fail-closed（必须在到时 reconcile 之前
    # 断言 —— reconcile 会把记录原地写成 inactive）。
    before_errors, _before_auth = _validation(
        deferred,
        dict(store.active_llm_pause(now=BASE + timedelta(seconds=2))),
        now=BASE + timedelta(seconds=2),
    )
    assert "global_pause_resume_receipt_not_inactive" in before_errors

    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()
    assert audit["active"] is False
    assert audit["resume_source"] == "provider_quota_reset_elapsed"

    # wedge 形状前提：另外两条 lane 在该形状下结构性不可用 ——
    # 无 marker（equal digest 冻结不写）、无蒸发投影（记录未被顶替）。
    assert not audit.get("last_suppressed_evidence_digest")
    assert not audit.get("last_suppressed_records")
    assert _evictions(audit) == []

    # 修复后：receipt lane（当前记录）授权 []，authorization=None
    # （receipt lane 不发 snapshot 授权事件）。
    errors, auth = _validation(deferred, audit, now=after_reset + timedelta(minutes=5))
    assert errors == [], errors
    assert auth is None


def test_exception_path_same_report_freeze_authorizes(isolated_store):
    """异常路径同报文冻结（入射投影 + 首次 persist）同样闭合。

    形状：``exc.pause_state()`` 先冻结（``observed_at``、无
    ``auto_resume_at``），再 persist —— 无既有记录 → 新 ACTIVE 记录
    digest == 冻结 digest；配额到时同样走 ``provider_quota_reset_elapsed``。
    """
    reset = BASE + timedelta(minutes=30)
    observed = BASE + timedelta(seconds=5)
    frozen = build_llm_pause_state(
        _quota_1308_issue(reset.isoformat()),
        role="WORKER 1308 same-report",
        observed_at=observed.isoformat(),
    )
    persisted = store.persist_llm_pause(frozen, now=observed)
    assert persisted["active"] is True
    assert persisted["evidence_digest"] == frozen["evidence_digest"], (
        "fresh persist makes the frozen (incoming) digest the record digest"
    )

    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()
    assert audit["resume_source"] == "provider_quota_reset_elapsed"
    assert not audit.get("last_suppressed_evidence_digest")
    assert _evictions(audit) == []

    errors, auth = _validation(frozen, audit, now=after_reset + timedelta(minutes=5))
    assert errors == [], errors
    assert auth is None


# ---------------------------------------------------------------------------
# (2) 风暴 churn —— 多轮 1308 循环后归档回执 lane 仍授权
# ---------------------------------------------------------------------------


def test_quota_storm_churn_rounds_still_authorize(isolated_store):
    """3 轮 trusted 1308 循环后：早轮冻结经归档回执 lane 授权，末轮经当前记录。"""
    moment = BASE
    frozen_rounds = []
    resets = []
    for index in range(3):
        round_reset = moment + timedelta(minutes=30)
        resets.append(round_reset)
        record = store.persist_llm_pause(
            _quota_1308_issue(round_reset.isoformat()),
            now=moment,
        )
        frozen_rounds.append(dict(record))
        after = round_reset + timedelta(seconds=1)
        assert store.active_llm_pause(now=after) is None
        moment = after

    audit = store.load_llm_pause()
    assert audit["active"] is False
    assert audit["resume_source"] == "provider_quota_reset_elapsed"
    # 早轮配额回执（provider_quota_reset_elapsed）被后续 1308 persist 归档。
    history = [
        entry
        for entry in (audit.get("resume_receipt_history") or [])
        if isinstance(entry, dict)
    ]
    assert len(history) == 2, history
    later = moment + timedelta(minutes=5)
    for index, frozen_round in enumerate(frozen_rounds):
        errors, _auth = _validation(frozen_round, audit, now=later)
        assert errors == [], f"round {index + 1} must authorize: {errors}"


# ---------------------------------------------------------------------------
# (3) 结构绑定 —— 伪造 digest / 伪造 category 仍拒绝
# ---------------------------------------------------------------------------


def test_forged_digest_still_rejected_on_quota_shape(isolated_store):
    reset = BASE + timedelta(minutes=30)
    record = store.persist_llm_pause(
        _quota_1308_issue(reset.isoformat()), now=BASE
    )
    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()

    forged = dict(record)
    forged["evidence_digest"] = hashlib.sha256(b"20261015-forged").hexdigest()
    errors, auth = _validation(forged, audit, now=after_reset + timedelta(minutes=5))
    assert errors
    assert "global_pause_resume_receipt_evidence_digest_mismatch" in errors
    assert auth is None


def test_real_digest_with_forged_category_still_rejected(isolated_store):
    reset = BASE + timedelta(minutes=30)
    record = store.persist_llm_pause(
        _quota_1308_issue(reset.isoformat()), now=BASE
    )
    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()

    mismatched = dict(record)
    mismatched["category"] = SERVICE_UNAVAILABLE
    errors, auth = _validation(
        mismatched, audit, now=after_reset + timedelta(minutes=5)
    )
    assert errors
    assert "global_pause_resume_receipt_category_mismatch" in errors
    assert auth is None


# ---------------------------------------------------------------------------
# (4) 原路径不回归 —— bounded_cooldown_elapsed 照旧；白名单不扩大
# ---------------------------------------------------------------------------


def test_bounded_cooldown_elapsed_path_unchanged(isolated_store):
    """service_unavailable（bounded_cooldown_elapsed）当前记录 + 归档 lane 照旧。"""
    pause = store.persist_llm_pause(
        _service_issue("HTTP 529 GLM 20261015 control"), now=BASE
    )
    deferred = dict(pause)
    due = store._parse_time(pause["auto_resume_at"])
    assert store.active_llm_pause(now=due + timedelta(seconds=1)) is None
    audit = store.load_llm_pause()
    assert audit["resume_source"] == "bounded_cooldown_elapsed"
    errors, auth = _validation(deferred, audit, now=due + timedelta(minutes=5))
    assert errors == [], errors
    assert auth is None

    # 后续 1308 覆写后：service 回执进入归档，归档 lane 照旧授权。
    reset = due + timedelta(minutes=10)
    store.persist_llm_pause(_quota_1308_issue(reset.isoformat()), now=due + timedelta(seconds=2))
    assert store.active_llm_pause(now=reset + timedelta(seconds=1)) is None
    audit2 = store.load_llm_pause()
    errors2, _auth2 = _validation(
        deferred, audit2, now=reset + timedelta(minutes=5)
    )
    assert errors2 == [], errors2


def test_only_system_transient_resume_sources_authorize():
    """白名单只放行两个系统自有 token —— 其余任何来源 fail-closed。"""
    digest = "a" * 64

    def _policy(resume_source: str):
        return durable._resume_receipt_policy_errors(
            {
                "category": QUOTA_429,
                "evidence_digest": digest,
                "retry_policy": "bounded_backoff",
                "http_status": 429,
                "requires_manual_resume": False,
                "resumed_at": (BASE + timedelta(hours=5)).isoformat(),
                "resume_source": resume_source,
                "resume_evidence_digest": None,
                "auto_resume_at": (BASE + timedelta(hours=5)).isoformat(),
            },
            digest=digest,
            manual=False,
            require_auto_resume_deadline=True,
        )

    for source in ("bounded_cooldown_elapsed", "provider_quota_reset_elapsed"):
        assert _policy(source) == [], source

    for source in (
        "untrusted_quota_pause_without_reset_authority",
        "operator_evidence_digest",
        "",
        "arbitrary_caller_supplied_source",
    ):
        errors = _policy(source)
        assert errors == ["transient_pause_cooldown_receipt_missing"], (
            f"{source!r} must stay fail-closed: {errors}"
        )


def test_untrusted_quota_drop_record_never_authorizes(isolated_store):
    """无信 1308 记录的强制清落（untrusted_quota_resume_source）端到端仍拒绝。

    预修复形状：bare-429/1302 时代发明的 5 小时等待（无 trusted reset
    authority）—— persist 路径把它强制转 inactive 并写
    ``resume_source="untrusted_quota_pause_without_reset_authority"``。
    该 token 绝不能因本次修复获得授权资格。
    """
    untrusted = store.persist_llm_pause(
        {
            "category": QUOTA_429,
            "summary": "guessed five-hour wait from a bare 429",
            "http_status": 429,
            "retry_policy": "manual_resume",
            "requires_manual_resume": True,
            "evidence_digest": "b" * 64,
            "quota_reset_authority": "",
        },
        now=BASE,
    )
    deferred = dict(untrusted)
    # 手工 billing 暂停覆写 → 无信 quota 记录被强制清落并归档其回执。
    billing = store.persist_llm_pause(
        {
            "category": "billing_cycle_usage_limit",
            "summary": "billing cycle exhausted",
            "http_status": 402,
            "retry_policy": "manual_resume",
            "requires_manual_resume": True,
            "evidence_digest": "c" * 64,
        },
        now=BASE + timedelta(minutes=1),
    )
    audit = store.load_llm_pause()
    history = [
        entry
        for entry in (audit.get("resume_receipt_history") or [])
        if isinstance(entry, dict)
    ]
    assert any(
        entry.get("resume_source") == "untrusted_quota_pause_without_reset_authority"
        and entry.get("evidence_digest") == untrusted["evidence_digest"]
        for entry in history
    ), "precondition: the untrusted quota drop receipt is archived"
    assert billing["active"] is True

    errors, auth = _validation(
        deferred, audit, now=BASE + timedelta(minutes=5)
    )
    assert errors
    assert "manual_pause_operator_receipt_missing" in errors
    assert auth is None
