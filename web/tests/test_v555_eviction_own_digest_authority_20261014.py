"""v555（2026-10-14）楔死根因的契约测试：被丢弃 ACTIVE 记录自身 digest 的蒸发投影.

事故序列（只读侦查证实，52 小时楔死 / 104 次慢车道复活全失败）：

1. store 持久化 P（``service_unavailable``）→ ACTIVE 记录持有 digest D；
2. Worker 在**同报文**上冻结 —— 冻结 digest 就是 D（抑制分支仅在
   ``digest != current.evidence_digest`` 时写 marker，
   ``llm_availability_store.py`` 同优先级同类别分支），因此 store 里
   没有任何 marker 记录 D；
3. 更高优先级的 1308 配额记录整体替换 P ——
   ``_suppressed_marker_eviction_projection`` 因 marker 为空直接
   ``return None``，D 在存储里零痕迹；
4. 配额 reset 后恢复校验（receipt 链 + marker 链）永久无门 →
   ``WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID`` 楔死。

修订（本文件钉住）：

* **store 主修**：蒸发投影把被丢弃 ACTIVE 记录自身的
  ``(category, evidence_digest)`` 并入扁平化 marker 列表（去重、最旧位），
  无 marker 的 ACTIVE 记录同样生成投影；
* **validator 加固**：``_marker_matches`` 增读归档蒸发投影**顶层**
  ``(category, evidence_digest)`` —— 覆盖 (a) 已落盘的 pre-v555 蒸发条目
  （其列表只有被压制 marker，顶层对是被丢弃记录自身 digest），以及
  (b) 34 槽列表界把自身（最旧）条目切回出去的形状（顶层对不受列表界
  约束、随 ``_carried_eviction_entry`` 原样转发）。

不变式（结构绑定 / fail-closed）：

* 只有 store 持有过的记录 digest 才入档 —— 伪造 digest（或真 digest ×
  伪造 category）在任何表面（列表、顶层对）都匹配不到；
* 蒸发投影不带 ``resumed_at`` / ``resume_source`` —— receipt 链对该投影
  永远关闭，授权只能走压制链的 horizon 门。
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


def _quota_1308_issue(reset_iso: str):
    body = (
        "Request rejected (429) · [1308][已达到 5 小时的使用上限。"
        f"您的限额将在 {reset_iso} 重置。]"
    )
    issue = classify_llm_availability([body], statuses=[429])
    assert issue is not None and issue.category == QUOTA_429
    return issue


def _errors(frozen: dict, audit: dict, now: datetime):
    return durable._worker_availability_resume_receipt_errors(frozen, audit, now=now)


def _validation(frozen: dict, audit: dict, now: datetime):
    return durable._worker_availability_resume_validation(frozen, audit, now=now)


def _evictions(audit: dict) -> list[dict]:
    history = audit.get("resume_receipt_history") or []
    return [
        entry
        for entry in history
        if isinstance(entry, dict)
        and entry.get("archive_kind") == "suppressed_marker_eviction"
    ]


def _marker_digests(entry: dict) -> list[str]:
    raw = entry.get("last_suppressed_records")
    if not isinstance(raw, list):
        return []
    return [
        str(item.get("evidence_digest") or "")
        for item in raw
        if isinstance(item, dict)
    ]


def _v555_sequence(*, observed_at: datetime | None = None, reset_at: datetime):
    """重建 v555 事故序列，返回 (frozen, quota_record)。

    1. P（service_unavailable）持久化为 ACTIVE 记录（digest D）；
    2. Worker 同报文冻结（异常路径形状：``observed_at``、无
       ``auto_resume_at``）—— 冻结 digest == D；
    3. 把冻结投影再 persist 一次（证明同 digest 抑制分支不写 marker）；
    4. 1308 配额（reset_at）在记录仍 ACTIVE 时整体替换。
    """
    text = "HTTP 529 GLM v555 incident stream failed reqid=555"
    record = store.persist_llm_pause(_service_issue(text), now=BASE)
    assert record["active"] is True

    observed = observed_at or BASE + timedelta(seconds=30)
    frozen = build_llm_pause_state(
        _service_issue(text), role="WORKER v555", observed_at=observed.isoformat()
    )
    assert frozen["evidence_digest"] == record["evidence_digest"], (
        "same report must freeze the ACTIVE record's own digest"
    )

    # 同 digest 再 persist：抑制分支不写任何 marker（v555 的第一半根因）。
    repersisted = store.persist_llm_pause(frozen, now=observed)
    assert not repersisted.get("last_suppressed_evidence_digest"), (
        "the equal-digest suppression branch writes no marker"
    )
    assert not repersisted.get("last_suppressed_records")

    quota = store.persist_llm_pause(
        _quota_1308_issue(reset_at.isoformat()), now=observed + timedelta(seconds=1)
    )
    assert quota["category"] == QUOTA_429
    return frozen, quota


# ---------------------------------------------------------------------------
# (b) v555 全序列回放 —— 越过配额 reset + horizon 后授权
# ---------------------------------------------------------------------------


def test_v555_full_sequence_authorizes_through_own_digest_eviction(isolated_store):
    """persist P → 同 digest 冻结 → 1308 整体替换 → reset+horizon 后 [] 授权。"""
    reset = BASE + timedelta(minutes=30)
    frozen, quota = _v555_sequence(reset_at=reset)

    # 1308 替换后：新 quota 记录自身无 marker，归档恰一条蒸发投影，
    # 其扁平化列表含被丢弃 ACTIVE 记录自身 digest（v555 主修）。
    ondisk = store.load_llm_pause()
    assert ondisk["evidence_digest"] == quota["evidence_digest"]
    assert "last_suppressed_evidence_digest" not in ondisk
    assert "last_suppressed_records" not in ondisk
    entries = _evictions(ondisk)
    assert len(entries) == 1
    eviction = entries[0]
    assert eviction.get("evidence_digest") == frozen["evidence_digest"]
    assert eviction.get("category") == SERVICE_UNAVAILABLE
    assert frozen["evidence_digest"] in _marker_digests(eviction)

    # 越过配额 reset → 记录 inactive → 恢复校验授权，matched_record 指向归档。
    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()
    assert audit["active"] is False
    later = after_reset + timedelta(minutes=5)
    errors, auth = _validation(frozen, audit, later)
    assert errors == [], errors
    assert auth is not None
    assert auth["authorization_shape"] == "suppressed_evidence_chain"
    assert auth["matched_record"] == "archived"


def test_v555_claim_boundary_freeze_shape_also_authorizes(isolated_store):
    """claim-boundary 冻结形状（冻结 = 已持久化记录投影，含 auto_resume_at）。"""
    text = "HTTP 529 GLM v555 claim-boundary stream failed reqid=556"
    record = store.persist_llm_pause(_service_issue(text), now=BASE)
    deferred = dict(record)
    assert deferred["evidence_digest"] == record["evidence_digest"]

    reset = BASE + timedelta(minutes=30)
    store.persist_llm_pause(
        _quota_1308_issue(reset.isoformat()), now=BASE + timedelta(seconds=1)
    )
    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()
    errors, auth = _validation(deferred, audit, after_reset + timedelta(minutes=5))
    assert errors == [], errors
    assert auth is not None and auth["matched_record"] == "archived"


def test_v555_eviction_survives_carry_forward_churn(isolated_store):
    """蒸发投影（含自身 digest）随 resume_receipt_history 向前转发不丢失。

    这同时验证 ``_carried_eviction_entry`` 对新形状的转发扁平天然成立。
    """
    reset = BASE + timedelta(minutes=30)
    frozen, _quota = _v555_sequence(reset_at=reset)

    # 后续风暴循环：persist 后冷却（记录先 inactive 再被替换 —— 回执路径）。
    moment = reset + timedelta(seconds=2)
    for index in range(3):
        store.persist_llm_pause(
            _service_issue(f"HTTP 529 post-quota storm blip {index}"), now=moment
        )
        current = store.load_llm_pause()
        due = store._parse_time(current["auto_resume_at"])
        moment = due + timedelta(seconds=1)
        assert store.active_llm_pause(now=moment) is None

    audit = store.load_llm_pause()
    entries = _evictions(audit)
    assert entries, "the own-digest eviction must survive carry-forward churn"
    digests = {d for entry in entries for d in _marker_digests(entry)}
    top_level = {str(entry.get("evidence_digest") or "") for entry in entries}
    assert frozen["evidence_digest"] in digests | top_level
    assert _errors(frozen, audit, moment + timedelta(minutes=5)) == []


# ---------------------------------------------------------------------------
# (c) 结构绑定 —— 伪造 digest / 伪造 category 仍拒绝
# ---------------------------------------------------------------------------


def test_forged_digest_rejected_against_own_digest_eviction(isolated_store):
    """伪造 digest 在列表与顶层对两个表面都匹配不到 → fail-closed。"""
    reset = BASE + timedelta(minutes=30)
    _frozen, _quota = _v555_sequence(reset_at=reset)
    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()

    forged = dict(_frozen)
    forged["evidence_digest"] = hashlib.sha256(b"v555-forged-digest").hexdigest()
    errors, auth = _validation(forged, audit, after_reset + timedelta(minutes=5))
    assert errors
    assert "global_pause_resume_receipt_no_archived_match" in errors
    assert auth is None


def test_real_digest_with_forged_category_rejected(isolated_store):
    """真 digest × 伪造 category：顶层对读取同样要求 category 相等。"""
    reset = BASE + timedelta(minutes=30)
    frozen, _quota = _v555_sequence(reset_at=reset)
    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()

    mismatched = dict(frozen)
    mismatched["category"] = "transport_unavailable"
    errors, auth = _validation(mismatched, audit, after_reset + timedelta(minutes=5))
    assert errors
    assert auth is None


# ---------------------------------------------------------------------------
# (d) receipt 链对蒸发投影仍关闭
# ---------------------------------------------------------------------------


def test_receipt_lane_stays_closed_for_eviction_projection(isolated_store):
    """蒸发投影不带 resumed_at / resume_source —— receipt 链无法授权。

    直接断言 receipt 两个检查器对该投影的失败令牌，再用行为断言收口：
    horizon 未过时（归档里只有这条蒸发投影）校验必须失败 —— 若 receipt
    链能读通该投影，horizon 门就被绕过了。
    """
    reset = BASE + timedelta(seconds=60)
    observed = BASE + timedelta(seconds=30)
    frozen, _quota = _v555_sequence(observed_at=observed, reset_at=reset)
    ondisk = store.load_llm_pause()
    eviction = _evictions(ondisk)[0]

    # 直接令牌：identity（timestamp 缺失）与 policy（cooldown 回执缺失）。
    identity_errors = durable._resume_receipt_identity_errors(frozen, eviction)
    policy_errors = durable._resume_receipt_policy_errors(
        eviction,
        digest=frozen["evidence_digest"],
        manual=False,
        require_auto_resume_deadline=False,
    )
    assert "global_pause_resume_receipt_timestamp_missing" in identity_errors
    assert "transient_pause_cooldown_receipt_missing" in policy_errors

    # 行为断言：记录已 inactive 但 horizon（observed_at+120s）未过 → 拒绝。
    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()
    horizon = observed + timedelta(seconds=120)
    assert after_reset < horizon
    errors, auth = _validation(frozen, audit, after_reset + timedelta(seconds=1))
    assert errors, "the receipt lane must not authorize before the horizon"
    assert "global_pause_resume_receipt_no_archived_match" in errors
    assert auth is None


# ---------------------------------------------------------------------------
# validator 加固：pre-v555 已落盘蒸发条目（列表无自身 digest）经顶层对授权
# ---------------------------------------------------------------------------


def test_pre_v555_on_disk_eviction_entry_authorizes_via_top_level_pair(
    isolated_store,
):
    """旧生产者写入的蒸发条目：列表只含真实压制 marker，顶层对是自身 digest。

    用带 marker 的旧形状（真实历史落盘状态）模拟：手工把自身 digest 从
    列表中移除、顶层对保留 —— validator 的顶层读取必须授权。34 槽列表界
    把自身（最旧）条目切出的形状与此完全同构（顶层对不受列表界约束）。
    """
    text = "HTTP 529 GLM v555 legacy disk record reqid=557"
    record = store.persist_llm_pause(_service_issue(text), now=BASE)
    suppressed = build_llm_pause_state(
        _service_issue("HTTP 529 other worker freeze reqid=558"),
        role="WORKER legacy",
        observed_at=(BASE + timedelta(seconds=5)).isoformat(),
    )
    store.persist_llm_pause(suppressed, now=BASE + timedelta(seconds=5))

    reset = BASE + timedelta(minutes=30)
    store.persist_llm_pause(
        _quota_1308_issue(reset.isoformat()), now=BASE + timedelta(seconds=6)
    )
    ondisk = store.load_llm_pause()
    entries = _evictions(ondisk)
    assert len(entries) == 1
    eviction = entries[0]
    # 新生产者形状：自身 digest 在列表最旧位。
    assert _marker_digests(eviction)[0] == record["evidence_digest"]
    assert suppressed["evidence_digest"] in _marker_digests(eviction)

    # 模拟 pre-v555 落盘形状：列表只留真实压制 marker，顶层对保留自身 digest。
    legacy_entry = dict(eviction)
    legacy_entry["last_suppressed_records"] = [
        item
        for item in eviction.get("last_suppressed_records") or []
        if isinstance(item, dict)
        and item.get("evidence_digest") != record["evidence_digest"]
    ]
    assert legacy_entry["last_suppressed_records"], "marker leg must remain"
    assert record["evidence_digest"] not in _marker_digests(legacy_entry)
    assert legacy_entry.get("evidence_digest") == record["evidence_digest"]

    after_reset = reset + timedelta(seconds=1)
    assert store.active_llm_pause(now=after_reset) is None
    audit = store.load_llm_pause()
    audit["resume_receipt_history"] = [legacy_entry]
    later = after_reset + timedelta(minutes=5)

    # 同报文冻结自身 digest 的效应：只能经顶层对授权。
    frozen = build_llm_pause_state(
        _service_issue(text),
        role="WORKER v555",
        observed_at=BASE.isoformat(),
    )
    assert frozen["evidence_digest"] == record["evidence_digest"]
    errors, auth = _validation(frozen, audit, later)
    assert errors == [], errors
    assert auth is not None
    assert auth["matched_record"] == "archived"

    # 被压制 marker 一侧不受影响：通过列表授权照旧。
    errors2, auth2 = _validation(suppressed, audit, later)
    assert errors2 == [], errors2
    assert auth2 is not None

    # 负对照：把顶层对也清掉的纯旧形状（顶层为空、列表无自身 digest）
    # —— 该表面既无据即拒，fail-closed 语义不变。
    stripped = dict(legacy_entry)
    stripped["evidence_digest"] = ""
    stripped["category"] = ""
    audit["resume_receipt_history"] = [stripped]
    errors3, auth3 = _validation(frozen, audit, later)
    assert errors3
    assert auth3 is None
