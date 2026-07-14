"""Adaptive concurrency control for LLM calls under API rate-limiting / backoff.

当 cc-switch gateway(127.0.0.1:15721, 聚合多供应商含 GLM)返回 503「所有供应商
已熔断」/ 529 overloaded / 429 配额上限时，全局降低并发上限——避免 N 个 agent
(worker + analyst) 同时重试放大压力导致雪崩。持续正常时逐步恢复。

NOTE(root-cause-audit 2026-06-21): 历史日志曾因把筹码增量中的数字误判为
HTTP 状态而夸大 503；真实 retry 风暴主因是 claude_agent_sdk signature
反序列化 bug。本模块机制（防御性降级）仍合理，保留。

设计：单一全局 level(线程/asyncio 安全)。
  - 每次 rate-limited 失败：level += 1（最多 3）
  - 连续 _RECOVER_SUCCESSES 次成功 且 退避保持期过后：level -= 1
  - get_adaptive_limit(base) = max(1, base >> level)  # level 0=base, 1=base/2, 2=base/4 ...

接入点：
  - llm_query.run_claude_query：检测到限速错误 → record_llm_outcome(success=False, rate_limited=True)；
    正常完成 → record_llm_outcome(success=True)。
  - agent_workers：取并发上限时用 get_adaptive_limit(BASE) 而非常量。
"""
import threading
import time

_LOCK = threading.Lock()
_level = 0                   # 0 = 满并发(base)，越大越保守
_last_failure_ts = 0.0       # 最近一次限速失败的时间戳
_consecutive_success = 0     # 连续成功计数(用于恢复)

# 连续成功多少次后恢复一级并发
_RECOVER_SUCCESSES = 8
# 限速失败后，至少保持当前退避级别多久(秒)才开始恢复——给供应商喘息时间
_BACKOFF_HOLD_SEC = 60.0
# 硬超时重置：距上次限速失败超过此时长则强制回 level 0。root-cause-audit 2026-06-21:
# 原逻辑 level 升只需 1 次失败、降需 8 次成功+60s hold，503 风暴后即便 API 恢复也要
# ~24 次串行成功(~80min)才回 level 0，期间 worker 并发 3→1 吞吐坍塌。reset() 无调用方。
_LEVEL_RESET_SEC = 1800.0
# 最高退避级别(level 3 = base//8)
_MAX_LEVEL = 3


def record_llm_outcome(success: bool, rate_limited: bool = False) -> None:
    """上报一次 LLM 调用的结果。

    success=False 且 rate_limited=True(503/529/429)→ 升一级退避(降并发)。
    success=True → 累计连续成功，达阈值且过保持期则降一级退避(恢复并发)。
    非 rate-limited 的失败(业务/网络错)不计入——只对 API 过载反应。
    """
    global _level, _last_failure_ts, _consecutive_success
    with _LOCK:
        if not success and rate_limited:
            if _level < _MAX_LEVEL:
                _level += 1
            _last_failure_ts = time.time()
            _consecutive_success = 0
        elif success:
            _consecutive_success += 1
            if (_level > 0
                    and _consecutive_success >= _RECOVER_SUCCESSES
                    and (time.time() - _last_failure_ts) >= _BACKOFF_HOLD_SEC):
                _level -= 1
                _consecutive_success = 0


def get_adaptive_limit(base: int) -> int:
    """返回当前应使用的并发上限。base 在退避期被指数缩减(右移 level 位)。"""
    global _level, _consecutive_success
    with _LOCK:
        # 硬超时重置：距上次限速失败 > _LEVEL_RESET_SEC 则强制回 level 0，防风暴后长期吞吐坍塌。
        if _level > 0 and (time.time() - _last_failure_ts) >= _LEVEL_RESET_SEC:
            _level = 0
            _consecutive_success = 0
        return max(1, base >> _level)


def get_level() -> int:
    """当前退避级别(0=正常，>0=退避中)。供观测/日志用。"""
    with _LOCK:
        return _level


def reset() -> None:
    """重置退避状态(测试用，或手动确认 API 恢复)。"""
    global _level, _last_failure_ts, _consecutive_success
    with _LOCK:
        _level = 0
        _last_failure_ts = 0.0
        _consecutive_success = 0
