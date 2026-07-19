"""Static cross-language guards for the strict dashboard authority contract."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "web" / "frontend" / "src"


def _quoted_array(source: str, constant: str) -> list[str]:
    match = re.search(
        rf"export const {re.escape(constant)} = \[(.*?)\] as const;",
        source,
        re.DOTALL,
    )
    assert match is not None, f"missing frontend constant {constant}"
    return re.findall(r'"([a-z0-9_]+)"', match.group(1))


def test_frontend_pipeline_stage_contract_matches_backend_order():
    from pipeline_state import STAGE_ORDER, session_recoverable_stages

    source = (FRONTEND / "constants" / "pipeline.ts").read_text(encoding="utf-8")

    assert _quoted_array(source, "PIPELINE_STAGE_CONTRACT") == STAGE_ORDER
    mapping = re.search(
        r"export const STAGE_TO_MILESTONE:.*?= \{(.*?)\n\};",
        source,
        re.DOTALL,
    )
    assert mapping is not None
    mapped_stages = re.findall(r"^\s{2}([a-z0-9_]+):", mapping.group(1), re.MULTILINE)
    assert mapped_stages == STAGE_ORDER

    timeout_leases = _quoted_array(
        source,
        "PIPELINE_TIMEOUT_LEASE_STAGE_CONTRACT",
    )
    assert timeout_leases == ["timed_out", "infra_timed_out"]
    assert set(timeout_leases).isdisjoint(STAGE_ORDER)
    assert set(timeout_leases) == set(session_recoverable_stages()) - set(STAGE_ORDER)
    assert 'nextTool: "abandon_generation"' in source
    assert 'nextTool: "run_precommit_eval"' in source

    component = (
        FRONTEND / "components" / "evolution" / "PipelineStatus.tsx"
    ).read_text(encoding="utf-8")
    assert "isPipelineTimeoutLeaseStage(rawStage)" in component
    assert "该租约不计入成功流水线进度" in component


def test_frontend_has_no_retired_certification_launcher():
    client = (FRONTEND / "api" / "client.ts").read_text(encoding="utf-8")
    progress = (
        FRONTEND / "components" / "evolution" / "OfficialCertificationProgress.tsx"
    ).read_text(encoding="utf-8")

    assert "enqueueFullCertification" not in client
    assert "enqueueCertification" not in client
    assert "postJSON" not in client
    assert "/enqueue" not in client
    assert "certificationJobs" in client
    assert ("Certification" + "Queue") not in client
    assert "/cancel" not in progress
    assert "cancelCertification" not in client
    assert 'stage === "official_bootstrap_required"' in progress
    assert '"operator_bootstrap_full_v5_job"' in progress
    assert "row.read_only === true" in progress
    assert "row.cancel_allowed === false" in progress


def test_dashboard_operator_contract_distinguishes_authority_shapes():
    runbook = (
        ROOT / "docs" / "evolution-continuous-delivery-runbook.md"
    ).read_text(encoding="utf-8")

    for endpoint in (
        "GET /api/pipeline/checkpoint",
        "GET /api/control/health",
        "GET /api/evolution/state",
    ):
        assert endpoint in runbook
    assert "handoff journal's `record_revision`" in runbook
    assert "`source_v`, `parent2_v`" in runbook
    assert "configuration intent" in runbook
    assert "effective live availability" in runbook
    assert "API-only operator/recovery" in runbook
    assert "Dashboard intentionally exposes no cancel control" in runbook
    assert "Compatibility fields such as `total_games`" in runbook
    assert "`bot_namespace.strict_generation_identity`" in runbook
    assert "Sorting, filtering, reaping" in runbook
    assert "must never calculate the ordinal" in runbook


def test_operator_token_is_memory_only_and_shared_across_mutations():
    source = (FRONTEND / "api" / "operatorControl.ts").read_text(encoding="utf-8")

    assert "X-Control-Token" in source
    assert "localStorage." not in source
    assert "sessionStorage." not in source
    assert "X-Arena-Token" not in source


def test_frontend_drops_stream_and_cycle_state_instead_of_merging_stale_authority():
    provider = (FRONTEND / "context" / "DataProvider.tsx").read_text(encoding="utf-8")
    evolution_api = (FRONTEND / "api" / "evolution.ts").read_text(encoding="utf-8")
    monitor = (FRONTEND / "pages" / "EvolutionMonitor.tsx").read_text(encoding="utf-8")
    logs = (FRONTEND / "pages" / "Logs.tsx").read_text(encoding="utf-8")

    assert 'addEventListener("epoch_blocked"' in provider
    assert "if (authorityBlocked) return" in provider
    assert "if (authorityBlocked) return" in evolution_api
    assert "existing?.selection_score" not in monitor
    assert "existing?.leaderboard_score" not in monitor
    assert "!visibleGenerations.some" in logs
    assert 'setLogContent("")' in logs
    assert "if (!cancelled) setLogContent(res.content)" in logs


def test_active_navigation_describes_read_only_contracts():
    sidebar = (FRONTEND / "layout" / "AppSidebar.tsx").read_text(encoding="utf-8")

    assert 'name: "严格发布 Bot"' in sidebar
    assert 'name: "提示词契约"' in sidebar
    assert "提示词编辑器" not in sidebar
    assert 'name: "Bot 管理"' not in sidebar


def test_control_observation_pairs_status_health_without_overlapping_polls():
    api = (FRONTEND / "api" / "control.ts").read_text(encoding="utf-8")
    client = (FRONTEND / "api" / "client.ts").read_text(encoding="utf-8")
    hook = (FRONTEND / "hooks" / "useControlStatus.ts").read_text(encoding="utf-8")
    panel = (FRONTEND / "pages" / "ControlPanel.tsx").read_text(encoding="utf-8")

    assert "export interface ControlHealth" in api
    assert "export interface PipelineRoute" in api
    assert "health: (signal?: AbortSignal)" in api
    assert 'Pick<AppConfig, "daemon_enabled" | "daemon_workers" | "daemon_pairs">' in api
    assert "const nextHealth = await controlApi.health()" in hook
    assert "const nextStatus = nextHealth.status" in hook
    assert "controlApi.status()" not in hook
    assert "if (inFlight.current) return inFlight.current" in hook
    assert "window.setTimeout(tick, pollMs)" in hook
    assert "setInterval" not in hook
    assert 'health?.overall === "healthy"' in panel
    assert "controlTaskActive(health?.task)" in panel
    assert "controlTaskStopping(health?.task)" in panel
    assert "停止中（任务仍持有运行权威）" in panel
    assert "runtimeMutationLocked" in panel
    assert "routeMatchesGeneration" in panel
    assert "页面不从 stage 猜测下一工具" in panel
    assert "controlStartBlockedReason(status, health)" in panel
    assert "route.parent2_v === status.active_generation.parent2_v" in panel
    assert "pipeline.parent2_v === active.parent2_v" in api
    assert "pipeline.handoff_owner_scope" in panel
    assert 'stage.endsWith("_rejected")' in (
        FRONTEND / "components" / "evolution" / "PipelineStatus.tsx"
    ).read_text(encoding="utf-8")
    assert "terminalization_pending" in api
    assert "clearOrchestratorSession" not in client
    assert "重置会话" not in panel
    assert "opaque session ID 不构成恢复权威" in panel
    assert "max={8}" in panel
    assert "Math.min(8," in panel
    assert "写入 evaluation identity 的完整 70 手样本预算（1–8）" in panel
    assert "它本身不是 Bot 强度证明" in panel


def test_frontend_liveness_fails_closed_on_sse_and_daemon_health():
    api = (FRONTEND / "api" / "control.ts").read_text(encoding="utf-8")
    provider = (FRONTEND / "context" / "DataProvider.tsx").read_text(encoding="utf-8")
    overview = (FRONTEND / "pages" / "Overview.tsx").read_text(encoding="utf-8")
    monitor = (FRONTEND / "pages" / "EvolutionMonitor.tsx").read_text(encoding="utf-8")
    evolution_api = (FRONTEND / "api" / "evolution.ts").read_text(encoding="utf-8")
    tool_card = (FRONTEND / "components" / "evolution" / "ToolCard.tsx").read_text(encoding="utf-8")

    assert 'state: "disconnected"' in provider
    assert "daemon: null" in provider
    assert 'addEventListener("ping"' in provider
    assert "dataStreamFresh" in overview
    assert 'heartbeat_status === "fresh"' in overview
    assert "daemonActuallyHealthy" in overview
    assert "process_identity" in api
    assert "配置意图：" in overview and "实际进程：" in overview
    assert "onDisconnect" in evolution_api
    assert "streamState !== \"connected\"" in monitor
    assert "运行标志存在但任务未活动" in monitor
    assert "编排器运行，等待下一动作" in monitor
    assert "interrupted: true" in monitor
    assert "状态未知（流中断）" in tool_card
    assert "最近一次发布完成" in monitor
    assert ">成功率<" not in monitor


def test_pipeline_component_validates_identity_and_does_not_greenwash_repair_or_critic():
    source = (FRONTEND / "components" / "evolution" / "PipelineStatus.tsx").read_text(encoding="utf-8")
    types = (FRONTEND / "api" / "types.ts").read_text(encoding="utf-8")
    presentation = (FRONTEND / "lib" / "pipelinePresentation.ts").read_text(encoding="utf-8")
    labels = (FRONTEND / "constants" / "pipeline.ts").read_text(encoding="utf-8")

    assert "checkpoint.master_plan.tasks" in source
    for field in ("evaluation_epoch", "next_v", "source_v", "parent2_v", "stage", "workflow_run_id", "run_id", "checkpoint_revision"):
        assert f"checkpoint.{field}" in presentation
    assert "activeGeneration.canonical_version" in presentation
    assert "canonicalGenerationIdentityIssues(activeGeneration)" in presentation
    assert "checkpoint_revision: number" in types
    assert "gate.advisory_approved === true" in presentation
    assert "gate.approved === true ? \"建议支持\"" not in source
    assert 'stage === "repair_planned" || stage === "rework_running"' in source
    assert "此前 quality/review/Critic/precommit 结果只描述修复前字节" in source
    assert "仅供后续决策参考，不授予发布资格" in source
    assert 'critic_checked: "建议性 Critic 已完成"' in labels
    assert "策略审核通过" not in labels


def test_official_ui_distinguishes_first_strict_control_from_normal_full_profile():
    source = (FRONTEND / "components" / "evolution" / "OfficialCertificationProgress.tsx").read_text(encoding="utf-8")
    bots = (FRONTEND / "pages" / "BotManager.tsx").read_text(encoding="utf-8")

    for token in (
        "operator_transition",
        "first_strict_control_v1",
        "system_control",
        "strength_evidence_weight === 0",
        "strategy_evidence_weight === 0",
        "official_status",
        "compliance_verdict",
        "certificate_digest",
        "ready_to_finalize",
    ):
        assert token in source
    assert source.index("jobsProjection?.operator_transition") < source.index("status.operator_transition")
    assert "5 轮自对弈 + 3 轮合格 strict 对手 × 70 手" in source
    assert "这是受控暂停，不是认证失败" in source
    assert "passed=false" in source
    assert "零强度权重" in source
    assert "first_strict_control_v1" in bots
    assert "强度与策略证据权重均为 0" in bots
    assert 'certification.certification_profile === "official-full-v5"' in bots
    assert 'certification.opponent_authority === "strict_published_pool"' in bots
    assert "published attestation:" in bots
    assert "verdict-ledger identity:" in bots
    assert "ledgerEntry?.certificate_digest === certification.certificate_digest" in bots
    assert 'ledgerEntry?.outcome === "official-certified"' in bots
    assert "正式证书身份投影不完整" in bots
    assert "不猜测为普通 5+3" in bots


def test_bot_page_consumes_backend_dual_identity_without_reindexing_or_tag_synthesis():
    bots = (FRONTEND / "pages" / "BotManager.tsx").read_text(encoding="utf-8")

    assert "displayOrdinalByName" not in bots
    assert ".map((bot, index) => [bot.name, index + 1] as const)" not in bots
    assert "displayIdentityByName" in bots
    assert "validatedPublishedIdentity" in bots
    assert "sameCanonicalGenerationIdentity(bot, authority)" in bots
    assert "第{identity.generation_ordinal}代" in bots
    assert "tag: {completionTag}" in bots
    assert "`national-bot-v${bot.version}`" not in bots
    assert "identity?.canonical_tag" in bots
    assert "排序和过滤不会重编号" in bots


def test_active_generation_views_render_backend_owned_dual_identity():
    control = (FRONTEND / "pages" / "ControlPanel.tsx").read_text(encoding="utf-8")
    epoch = (
        FRONTEND / "components" / "evolution" / "EpochAuthorityStatus.tsx"
    ).read_text(encoding="utf-8")
    pipeline = (
        FRONTEND / "components" / "evolution" / "PipelineStatus.tsx"
    ).read_text(encoding="utf-8")
    helper = (
        FRONTEND / "lib" / "canonicalGenerationIdentity.ts"
    ).read_text(encoding="utf-8")

    for source in (control, epoch, pipeline):
        assert "canonicalGenerationLabel" in source
        assert "双身份投影不可用" in source
    assert "version -" not in helper
    assert "generation_ordinal +" not in helper
    assert "canonical_tag}" in helper


def test_official_ui_preserves_jobless_digest_bound_bootstrap_failure_and_normal_stage_jobs():
    source = (FRONTEND / "components" / "evolution" / "OfficialCertificationProgress.tsx").read_text(encoding="utf-8")

    assert "transitionJobBindingValid" in source
    assert 'transition.state === "bootstrap_failed"' in source
    assert "transition.job_id" in source and ": !job" in source
    assert "failureProjectionValid" in source
    assert "transition.certificate_digest === job.certificate_digest" in source
    assert "原因：{transition.reason" in source
    assert "动作：{transition.action}" in source
    assert "digest-bound transition" in source
    for stage in (
        "official_certifying",
        "official_failed",
        "official_inconclusive",
        "publishing",
    ):
        assert f'"{stage}"' in source
    assert "NORMAL_JOB_STAGES.has(stage)" in source
    assert "不从 stage 反推证书或发布完成" in source


def test_v143_numeric_high_water_source_does_not_select_normal_certification_profile():
    # The real fresh checkpoint binds numeric high-water v142 as source_v while
    # explicitly carrying no inherited source artifact.  Frontend profile
    # selection must therefore consume the bound transition/job, not source_v.
    checkpoint_fixture = {"next_v": 143, "source_v": 142}
    source = (FRONTEND / "components" / "evolution" / "OfficialCertificationProgress.tsx").read_text(encoding="utf-8")

    assert checkpoint_fixture == {"next_v": 143, "source_v": 142}
    assert "generation.source_v == null" not in source
    assert "transition.source_v === 142" in source
    assert 'certificationProfile === "first_strict_control_v1"' in source
    assert 'job.certification_profile === "official-full-v5"' in source
    assert 'job.opponent_authority === "strict_published_pool"' in source
    assert "job.formal_profile?.self_play_rounds === 5" in source
    assert "job.formal_profile.opponent_rounds === 3" in source
    assert "job.formal_profile.target_hands === 70" in source
    assert "job.strength_evidence_weight === 0" in source
    assert "job.strategy_evidence_weight === 0" in source
    assert "不从版本号或 source_v 猜测" in source


def test_stability_ui_requires_fresh_background_verification_and_exact_state():
    source = (FRONTEND / "lib" / "stabilityView.ts").read_text(encoding="utf-8")

    for text in (
        "验证投影不可用",
        "连续性验证中",
        "连续性验证已过期",
        "连续性验证失败",
        "尚未开始",
        "已持久化归零",
        "观测中",
        "代次达标，等待强度周期",
        "连续验收完成",
    ):
        assert text in source
    assert 'verification.state !== "fresh"' in source


def test_control_hook_pairs_stability_and_full_checkpoint_revision_before_green():
    hook = (FRONTEND / "hooks" / "useControlStatus.ts").read_text(encoding="utf-8")
    panel = (FRONTEND / "pages" / "ControlPanel.tsx").read_text(encoding="utf-8")
    types = (FRONTEND / "api" / "control.ts").read_text(encoding="utf-8")

    assert "stability_observation_digest" in types
    assert "checkpoint_revision: number" in types
    assert "healthStatus.stability_observation_digest !== status.stability_observation_digest" in hook
    assert "const pipeline = health?.pipeline;" in panel
    for field in (
        "next_v",
        "source_v",
        "stage",
        "run_id",
        "workflow_run_id",
        "checkpoint_revision",
    ):
        assert f"health.pipeline.{field}" in hook
        assert f"pipeline.{field}" in panel


def test_unpublished_ui_does_not_guess_version_reusability():
    source = (FRONTEND / "components" / "evolution" / "EpochAuthorityStatus.tsx").read_text(encoding="utf-8")

    assert "已提交但未发布" in source
    assert "不能由此列表推断" in source
    assert "不占版本号" not in source


def test_dashboard_redesign_adds_structured_evolution_views():
    """The redesign adds structured views behind new routes, all consuming the
    shared normalization layer and paired health observation."""
    app = (FRONTEND / "App.tsx").read_text(encoding="utf-8")
    sidebar = (FRONTEND / "layout" / "AppSidebar.tsx").read_text(encoding="utf-8")

    for route in ("/pipeline", "/agents", "/evidence", "/bots-inventory", "/failures", "/strength"):
        assert f'path="{route}"' in app, f"missing redesigned view route {route}"
    for route in ("/", "/evolution", "/bots", "/prompts"):
        assert f'path="{route}"' in app
    assert 'group: "进化"' in sidebar
    assert 'name: "严格发布 Bot"' in sidebar
    assert 'name: "提示词契约"' in sidebar


def test_dashboard_redesign_api_clients_validate_and_fail_closed():
    client = (FRONTEND / "api" / "client.ts").read_text(encoding="utf-8")
    agents_validator = (FRONTEND / "api" / "agentActivity.ts").read_text(encoding="utf-8")
    strength_validator = (FRONTEND / "api" / "strengthJobs.ts").read_text(encoding="utf-8")

    assert "pipelineAgents" in client
    assert "pipelineStrengthJobs" in client
    assert "expectAgentActivity" in client
    assert "expectStrengthJobs" in client
    assert "structurally incomplete" in agents_validator
    assert "evaluation_identity_digest is invalid" in strength_validator
    assert "daemon health snapshot" in strength_validator


def test_dashboard_redesign_domain_layer_does_not_mix_authority_shapes():
    agent_view = (FRONTEND / "domain" / "agentActivityView.ts").read_text(encoding="utf-8")
    strength_view = (FRONTEND / "domain" / "strengthJobView.ts").read_text(encoding="utf-8")
    evidence = (FRONTEND / "domain" / "evidenceAuthority.ts").read_text(encoding="utf-8")
    failures = (FRONTEND / "domain" / "failureRecoveryView.ts").read_text(encoding="utf-8")

    assert "advisory" in agent_view
    assert "EVIDENCE_TIER_LABELS" in evidence
    for tier in ("compliance", "strength", "advisory", "diagnostic", "zero"):
        assert f'"{tier}"' in evidence
    assert "isPipelineTimeoutLeaseStage" in agent_view
    assert "stageIsTimeoutLease" in agent_view
    assert "configured_dead" in strength_view
    assert "alive_stale_heartbeat" in strength_view
    assert "strengthRejectionLabel" in strength_view
    for disposition in ("auto_retry", "awaiting_lease", "needs_repair", "authority_conflict", "operator_action", "terminal"):
        assert f'"{disposition}"' in failures


def test_backend_agent_and_strength_endpoints_are_read_only_authority_bound():
    pipeline_route = (ROOT / "web" / "server" / "routes" / "pipeline.py").read_text(encoding="utf-8")

    assert '@router.get("/agents")' in pipeline_route
    assert '@router.get("/strength-jobs")' in pipeline_route
    assert "load_strict_pipeline_checkpoint" in pipeline_route
    assert "load_strict_strength_snapshot" in pipeline_route
    assert "read_strict_worker_failures" in pipeline_route
    assert '"available": False' in pipeline_route
    assert '"critic_llm_executed") is True' in pipeline_route
    assert '"schema_valid") is True' in pipeline_route
