import assert from "node:assert/strict";
import test from "node:test";

import { expectAgentActivity } from "../node_modules/.tmp/sse-tests/api/agentActivity.js";
import {
  expectStrengthJobs,
  strengthJobsBindingIssues,
} from "../node_modules/.tmp/sse-tests/api/strengthJobs.js";
import {
  agentActivityView,
  agentRoleSummaries,
} from "../node_modules/.tmp/sse-tests/domain/agentActivityView.js";
import {
  strengthJobView,
  daemonLivenessView,
  strengthRejectionLabel,
  producerConsumerCapabilityView,
} from "../node_modules/.tmp/sse-tests/domain/strengthJobView.js";
import {
  evidenceTierForGate,
  evidenceTierForOfficialCertification,
  evidenceTierForBootstrapJob,
  criticAdvisoryVerdictLabel,
  EVIDENCE_TIER_LABELS,
} from "../node_modules/.tmp/sse-tests/domain/evidenceAuthority.js";
import {
  workerFailureRows,
  pipelineRecoveryRows,
} from "../node_modules/.tmp/sse-tests/domain/failureRecoveryView.js";
import { operatorSituationView } from "../node_modules/.tmp/sse-tests/domain/operatorSituationView.js";
import { pipelineStageProgress } from "../node_modules/.tmp/sse-tests/constants/pipeline.js";
import {
  isOfficialCertificationStage,
  isNormalOfficialCertificationStage,
} from "../node_modules/.tmp/sse-tests/api/officialJobs.js";

const ID64 = "a".repeat(64);
const OBSERVER = {
  complete: true,
  issues: [],
  usage: { directory_entries: 0, files_read: 1, total_read_bytes: 0, rows_seen: 0 },
  limits: { directory_entries: 256, files_read: 80, total_read_bytes: 8 * 1024 * 1024, cpu_seconds: 0.75, wall_seconds: 3, rows: 1000 },
};
const STRENGTH_CAPABILITIES = {
  durable_job_lifecycle: false,
  queued_running_leases: false,
  producer_consumer_dispatch: false,
};
const strengthAuthority = (activeBots = ["national_v143", "national_v144"]) => ({
  evaluation_epoch: "national_tcp_policy_v1",
  active_bots: activeBots,
  epoch_reset_receipt_digest: ID64,
  evaluation_identity_digest: ID64,
  evaluation_manifest_digest: ID64,
  complete: true,
});
const page = (admitted = 0, staged = 0, inadmissible = 0) => ({
  offset: 0, limit: 50,
  admitted_total: admitted,
  staged_pending_total: staged,
  inadmissible_total: inadmissible,
  admitted_has_more: false,
  staged_pending_has_more: false,
  inadmissible_has_more: false,
});

function baseProjection(overrides = {}) {
  return {
    available: true,
    evaluation_epoch: "national_tcp_policy_v1",
    workflow_run_id: "workflow-v2",
    run_id: "144#1",
    next_v: 144,
    source_v: 143,
    parent2_v: null,
    checkpoint_revision: 7,
    stage: "workers_done",
    attempts: { generation: 1, audit: 0, precommit: 0 },
    rework_counts: { worker_failure: 0, precommit: 0, official: 0 },
    orchestrator: { stage: "workers_done", reviewer_feedback: null, infra_failure: null, official_jobs_polling_supported: false },
    master: { started: true, completed: true, plan_present: true, analysis: null, tasks: [], task_total: 0, tasks_truncated: false },
    direction_audit: null,
    gates: {
      quality: null,
      review: null,
      critic: null,
      precommit_eval: null,
      official_full: null,
    },
    gate_keys_present: [],
    worker_failures: [],
    worker_failures_truncated: false,
    observer_limits: { max_tasks: 8, max_target_files_per_task: 8, max_worker_failures: 10, max_response_bytes: 64 * 1024 },
    ...overrides,
  };
}

test("expectAgentActivity accepts an unavailable workflow and rejects malformed shapes", () => {
  const unavailable = expectAgentActivity({
    available: false,
    reason: "no_strict_workflow",
    evaluation_epoch: "national_tcp_policy_v1",
  });
  assert.equal(unavailable.available, false);

  assert.throws(
    () => expectAgentActivity({ available: false, evaluation_epoch: "national_tcp_policy_v1" }),
    /missing a reason/,
  );
  assert.throws(
    () => expectAgentActivity({ available: false, reason: "x", evaluation_epoch: "other" }),
    /evaluation_epoch/,
  );
  assert.throws(() => expectAgentActivity(null), /not an object/);
  assert.throws(
    () => expectAgentActivity({ available: "yes", evaluation_epoch: "national_tcp_policy_v1" }),
    /neither true nor false/,
  );
});

test("expectAgentActivity rejects an available projection missing structural pieces", () => {
  assert.throws(
    () => expectAgentActivity(baseProjection({ attempts: null })),
    /structurally incomplete/,
  );
  assert.throws(
    () => expectAgentActivity(baseProjection({ master: null })),
    /structurally incomplete/,
  );
  assert.throws(
    () => expectAgentActivity(baseProjection({
      orchestrator: {
        stage: "workers_done",
        reviewer_feedback: null,
        infra_failure: null,
        official_jobs_polling_supported: true,
      },
    })),
    /nested contract/,
  );
  const official = baseProjection({
    stage: "official_certifying",
    orchestrator: {
      stage: "official_certifying",
      reviewer_feedback: null,
      infra_failure: null,
      official_jobs_polling_supported: true,
    },
  });
  assert.equal(expectAgentActivity(official).available, true);
  assert.equal(agentActivityView(official).officialJobsPollingSupported, true);
  const infra = baseProjection({
    orchestrator: {
      stage: "workers_done",
      reviewer_feedback: null,
      official_jobs_polling_supported: false,
      infra_failure: {
        schema_version: 1,
        failure_class: "infrastructure",
        component: "worker_llm",
        code: "worker_llm_unavailable",
        owner_tool: "run_workers",
        resume_stage: "master_planned",
        attempt: 1,
        max_attempts: 3,
        retryable: true,
        exhausted: false,
        action: "retry_same_tool",
      },
    },
  });
  assert.equal(expectAgentActivity(infra).available, true);
  assert.throws(
    () => expectAgentActivity({
      ...infra,
      orchestrator: {
        ...infra.orchestrator,
        infra_failure: { ...infra.orchestrator.infra_failure, raw_status: "secret" },
      },
    }),
    /nested contract/,
  );
});

test("agentRoleSummaries reports critic as advisory and not a strength gate", () => {
  const projection = baseProjection({
    stage: "critic_checked",
    gates: {
      quality: { name: "quality", present: true, complete: true, authority_state: "current", fields: {} },
      review: { name: "review", present: true, complete: true, authority_state: "current", fields: {} },
      critic: {
        name: "critic",
        present: true,
        complete: true,
        authority_state: "current",
        fields: { advisory_approved: false, advisory_score: 3 },
      },
      precommit_eval: null,
      official_full: null,
    },
  });
  const roles = agentRoleSummaries(projection);
  const critic = roles.find((r) => r.role === "critic");
  assert.equal(critic.state, "terminal");
  assert.match(critic.detail, /建议.*不单独决定/);
});

test("agentActivityView flags timed_out as a timeout lease, not a known pipeline stage", () => {
  const view = agentActivityView(baseProjection({ stage: "timed_out" }));
  assert.equal(view.available, true);
  assert.equal(view.stageIsTimeoutLease, true);
  // timed_out is not in PIPELINE_STAGE_CONTRACT but is a known timeout lease.
  assert.equal(view.stageKnown, true);
  const workers = view.roles.find((row) => row.role === "workers");
  assert.equal(workers.state, "unknown");
  assert.match(workers.detail, /不会把它倒退成未开始/);
});

test("agent role high-water survives timeout and review_rejected is terminal", () => {
  const currentGate = (name, complete) => ({
    name, present: true, complete, authority_state: "current", fields: {},
  });
  const timedOut = agentRoleSummaries(baseProjection({
    stage: "timed_out",
    gates: {
      quality: currentGate("quality", true),
      review: currentGate("review", true),
      critic: null,
      precommit_eval: null,
      official_full: null,
    },
  }));
  assert.equal(timedOut.find((row) => row.role === "workers").state, "terminal");
  assert.equal(timedOut.find((row) => row.role === "reviewer").state, "terminal");
  assert.equal(timedOut.find((row) => row.role === "critic").state, "unknown");

  const rejected = agentRoleSummaries(baseProjection({
    stage: "review_rejected",
    gates: {
      quality: currentGate("quality", true),
      review: currentGate("review", false),
      critic: null,
      precommit_eval: null,
      official_full: null,
    },
  }));
  const reviewer = rejected.find((row) => row.role === "reviewer");
  assert.equal(reviewer.state, "terminal");
  assert.match(reviewer.detail, /绑定拒绝结论/);
  assert.equal(rejected.find((row) => row.role === "critic").state, "not_reached");

  const historical = (name) => ({
    name, present: true, complete: false, authority_state: "historical_invalidated", fields: {},
  });
  const repairing = agentRoleSummaries(baseProjection({
    stage: "repair_planned",
    gates: {
      quality: historical("quality"),
      review: historical("review"),
      critic: historical("critic"),
      precommit_eval: null,
      official_full: null,
    },
  }));
  assert.equal(repairing.find((row) => row.role === "workers").state, "running");
  assert.equal(repairing.find((row) => row.role === "reviewer").state, "not_reached");
  assert.equal(repairing.find((row) => row.role === "critic").state, "not_reached");
});

test("checkpoint stage progress separates completed boundary from next work", () => {
  assert.deepEqual(pipelineStageProgress("direction_audited"), {
    kind: "completed_boundary",
    completedThrough: "direction_audited",
    activeMilestone: "master_planned",
  });
  assert.deepEqual(pipelineStageProgress("review_rejected"), {
    kind: "failed_boundary",
    completedThrough: "quality_passed",
    activeMilestone: "reviewed",
  });
  assert.deepEqual(pipelineStageProgress("official_certifying"), {
    kind: "in_progress",
    completedThrough: "verified",
    activeMilestone: "official_certifying",
  });
  assert.deepEqual(pipelineStageProgress("archived"), {
    kind: "completed_boundary",
    completedThrough: "publishing",
    activeMilestone: "archived",
  });
});

test("agentActivityView flags an unknown backend stage without guessing", () => {
  const view = agentActivityView(baseProjection({ stage: "made_up_stage" }));
  assert.equal(view.stageKnown, false);
  assert.equal(view.stageIsTimeoutLease, false);
});

test("agentActivityView returns unavailable shape unchanged", () => {
  const view = agentActivityView({
    available: false,
    reason: "no_strict_workflow",
    evaluation_epoch: "national_tcp_policy_v1",
  });
  assert.equal(view.available, false);
  assert.equal(view.reason, "no_strict_workflow");
});

test("expectStrengthJobs validates identity digest and daemon health", () => {
  const ok = expectStrengthJobs({
    available: true,
    evaluation_epoch: "national_tcp_policy_v1",
    evaluation_identity_digest: ID64,
    evaluation_manifest_digest: ID64,
    epoch_reset_receipt_digest: ID64,
    active_bots: ["national_v143", "national_v144"],
    capabilities: STRENGTH_CAPABILITIES,
    authority_binding: strengthAuthority(),
    daemon: { alive: true, configured: true, heartbeat_status: "fresh" },
    admitted_samples: [],
    staged_pending: [],
    inadmissible_diagnostics: [],
    pagination: page(),
    observer: OBSERVER,
    daemon_stats: {},
  });
  assert.equal(ok.available, true);

  assert.throws(
    () => expectStrengthJobs({
      available: true,
      evaluation_epoch: "national_tcp_policy_v1",
      evaluation_identity_digest: "short",
      evaluation_manifest_digest: ID64,
      epoch_reset_receipt_digest: ID64,
      active_bots: ["national_v143", "national_v144"],
      capabilities: STRENGTH_CAPABILITIES,
      authority_binding: strengthAuthority(),
      daemon: { alive: true },
      admitted_samples: [],
      staged_pending: [],
      inadmissible_diagnostics: [],
      pagination: page(),
      observer: OBSERVER,
      daemon_stats: {},
    }),
    /identity_digest is invalid/,
  );
  assert.throws(
    () => expectStrengthJobs({
      ...ok,
      capabilities: { ...STRENGTH_CAPABILITIES, unexpected: true },
    }),
    /authority binding is invalid/,
  );
  assert.throws(
    () => expectStrengthJobs({
      ...ok,
      authority_binding: { ...strengthAuthority(), unexpected: "field" },
    }),
    /authority binding is invalid/,
  );
  assert.throws(
    () => expectStrengthJobs({ available: true, evaluation_epoch: "national_tcp_policy_v1" }),
    /daemon health/,
  );
});

test("strengthJobView aggregates inadmissible rejection reason counts", () => {
  const view = strengthJobView({
    available: true,
    evaluation_epoch: "national_tcp_policy_v1",
    evaluation_identity_digest: ID64,
    evaluation_manifest_digest: ID64,
    epoch_reset_receipt_digest: ID64,
    active_bots: ["national_v143", "national_v144"],
    capabilities: STRENGTH_CAPABILITIES,
    authority_binding: strengthAuthority(),
    daemon: { alive: true, configured: true, heartbeat_status: "fresh", pid: 123, heartbeat_age_sec: 5 },
    admitted_samples: [{ id: "m1", bot0: "national_v143", bot1: "national_v144" }],
    staged_pending: [],
    inadmissible_diagnostics: [
      { id: "bad1", rejection_reasons: ["hands_per_strength_sample_not_70", "bot1_not_in_active_pool"] },
      { id: "bad2", rejection_reasons: ["hands_per_strength_sample_not_70"] },
    ],
    pagination: page(1, 0, 2),
    observer: OBSERVER,
    daemon_stats: {},
  });
  assert.equal(view.available, true);
  assert.equal(view.admittedCount, 1);
  const top = view.inadmissibleReasonCounts[0];
  assert.equal(top.reason, "hands_per_strength_sample_not_70");
  assert.equal(top.count, 2);
  assert.match(strengthRejectionLabel(top.reason), /非 70 手/);
});

test("daemonLivenessView distinguishes configured+alive from configured+dead", () => {
  const fresh = daemonLivenessView({ alive: true, configured: true, heartbeat_status: "fresh", pid: 1, heartbeat_age_sec: 2 });
  assert.equal(fresh.state, "alive_fresh");
  const dead = daemonLivenessView({ alive: false, configured: true });
  assert.equal(dead.state, "configured_dead");
  const unconfigured = daemonLivenessView({ alive: false, configured: false });
  assert.equal(unconfigured.state, "unconfigured");
  assert.match(unconfigured.detail, /配置未启用/);
});

test("strength authority binding rejects stale reset/pool and capability copy is backend-driven", () => {
  const response = {
    available: false,
    reason: "active_pool_singleton",
    evaluation_epoch: "national_tcp_policy_v1",
    active_bots: ["national_v143", "national_v144"],
    epoch_reset_receipt_digest: ID64,
    capabilities: STRENGTH_CAPABILITIES,
    authority_binding: strengthAuthority(),
    daemon: { alive: true, configured: true, heartbeat_status: "fresh" },
    observer: OBSERVER,
  };
  assert.deepEqual(strengthJobsBindingIssues(response, {
    active_bots: ["national_v143", "national_v144"],
    reset_receipt_digest: ID64,
  }), []);
  assert.ok(strengthJobsBindingIssues(response, {
    active_bots: ["national_v143"],
    reset_receipt_digest: ID64,
  }).includes("active_bots"));
  assert.ok(strengthJobsBindingIssues(response, {
    active_bots: ["national_v143", "national_v144"],
    reset_receipt_digest: "b".repeat(64),
  }).includes("epoch_reset_receipt_digest"));

  const disabled = producerConsumerCapabilityView(STRENGTH_CAPABILITIES);
  assert.equal(disabled.enabled, false);
  assert.match(disabled.label, /尚未启用/);
  const enabled = producerConsumerCapabilityView({
    durable_job_lifecycle: true,
    queued_running_leases: true,
    producer_consumer_dispatch: true,
  });
  assert.equal(enabled.enabled, true);
  assert.match(enabled.detail, /均可用/);
});

test("evidenceTierForGate classifies critic and official_full correctly", () => {
  assert.equal(evidenceTierForGate(null).tier, "zero");
  const critic = evidenceTierForGate({ name: "critic", present: true, complete: true, authority_state: "current", fields: {} });
  assert.equal(critic.tier, "advisory");
  const officialPassed = evidenceTierForGate({ name: "official_full", present: true, complete: true, authority_state: "current", fields: {} });
  assert.equal(officialPassed.tier, "compliance");
  const officialFailed = evidenceTierForGate({ name: "official_full", present: true, complete: false, authority_state: "current", fields: {} });
  assert.equal(officialFailed.tier, "zero");
  const reviewer = evidenceTierForGate({ name: "review", present: true, complete: true, authority_state: "current", fields: {} });
  assert.equal(reviewer.tier, "compliance");
});

test("evidenceTierForOfficialCertification only treats signed_full_v5 as compliance", () => {
  const signed = evidenceTierForOfficialCertification({ formal_certified: true, formal_authority: "signed_full_v5" });
  assert.equal(signed.tier, "compliance");
  const none = evidenceTierForOfficialCertification({ formal_certified: false, formal_authority: "none" });
  assert.equal(none.tier, "zero");
});

test("evidenceTierForOfficialCertification classifies staging tier correctly", () => {
  // Two-tier: a staging bot (published, awaiting async cert) is staging tier, not compliance or zero.
  const staging = evidenceTierForOfficialCertification({ publication_tier: "staging", formal_authority: "staging_uncertified" });
  assert.equal(staging.tier, "staging");
  // A certified bot is still compliance even if publication_tier is also set.
  const certified = evidenceTierForOfficialCertification({ publication_tier: "certified", formal_certified: true, formal_authority: "signed_full_v5" });
  assert.equal(certified.tier, "compliance");
});

test("official job rows remain zero-weight progress until a signed certificate is validated", () => {
  const normalJob = evidenceTierForBootstrapJob({ formal_authority: "pipeline_attached_full_v5_job" });
  const firstJob = evidenceTierForBootstrapJob({ formal_authority: "operator_bootstrap_full_v5_job" });
  assert.equal(normalJob.tier, "zero");
  assert.equal(firstJob.tier, "zero");
  assert.match(normalJob.label, /非证书.*零强度/);
});

test("criticAdvisoryVerdictLabel mirrors criticAdvisoryComplete field chain", () => {
  const complete = criticAdvisoryVerdictLabel({
    name: "critic",
    present: true,
    complete: true,
    authority_state: "current",
    fields: { approved: true, schema_valid: true, llm_invoked: true, critic_llm_executed: true, advisory_approved: true },
  });
  assert.equal(complete.complete, true);
  assert.equal(complete.verdict, "建议支持");

  const incomplete = criticAdvisoryVerdictLabel({
    name: "critic",
    present: true,
    complete: false,
    authority_state: "current",
    fields: { approved: true, schema_valid: true }, // missing llm chain
  });
  assert.equal(incomplete.complete, false);
});

test("EVIDENCE_TIER_LABELS exposes all six tiers", () => {
  const tiers = Object.keys(EVIDENCE_TIER_LABELS).sort();
  assert.deepEqual(tiers, ["advisory", "compliance", "diagnostic", "staging", "strength", "zero"]);
});

test("workerFailureRows keeps the backend category without re-deriving it", () => {
  const rows = workerFailureRows([
    { worker_id: 1, role: "Tuner", error: "boom", failure_type: "x", category: "worker", gen: 144 },
    { worker_id: 2, role: "Gate", error: "kaboom", failure_type: "y", category: "gate", gen: 144 },
    { worker_id: 3, role: "Unknown", error: "huh", failure_type: "z", category: "other", gen: 144 },
  ]);
  assert.equal(rows.length, 3);
  assert.equal(rows[0].failureClass, "worker");
  assert.equal(rows[1].failureClass, "gate");
  assert.equal(rows[2].failureClass, "unknown");
  assert.equal(rows[0].disposition, "historical");
  assert.match(rows[0].dispositionLabel, /历史失败记录/);
});

test("pipelineRecoveryRows makes timeout leases first-class and route-bound", () => {
  const pipeline = {
    exists: true,
    stage: "infra_timed_out",
    next_v: 144,
    source_v: 143,
    parent2_v: null,
    route: {
      stage: "infra_timed_out",
      next_v: 144,
      source_v: 143,
      parent2_v: null,
      next_tool: "run_precommit_eval",
      allowed_tools: ["run_precommit_eval"],
      intent: "recovery",
      directive: "resume exact native precommit",
    },
  };
  const [lease] = pipelineRecoveryRows(pipeline, null);
  assert.equal(lease.failureClass, "timeout_lease");
  assert.equal(lease.disposition, "awaiting_lease");
  assert.equal(lease.ownerTool, "run_precommit_eval");
  assert.match(lease.dispositionLabel, /不会静默等待/);

  const [unbound] = pipelineRecoveryRows({ ...pipeline, route: null }, null);
  assert.equal(unbound.ownerTool, null);
  assert.match(unbound.dispositionLabel, /拒绝猜测/);

  const [ordinaryInfra] = pipelineRecoveryRows(
    { exists: true, stage: "workers_done" },
    { action: "await_lease", attempt: 1, max_attempts: 3 },
  );
  assert.equal(ordinaryInfra.failureClass, "infrastructure");
  assert.equal(ordinaryInfra.disposition, "operator_action");
});

test("official job polling eligibility is restricted to certification boundaries", () => {
  for (const stage of ["selected", "direction_audited", "workers_done", "verified", "publishing", "archived"]) {
    assert.equal(isOfficialCertificationStage(stage), false, stage);
  }
  for (const stage of ["official_bootstrap_required", "official_certifying", "official_failed", "official_inconclusive"]) {
    assert.equal(isOfficialCertificationStage(stage), true, stage);
  }
  assert.equal(isNormalOfficialCertificationStage("official_bootstrap_required"), false);
  assert.equal(isNormalOfficialCertificationStage("official_certifying"), true);
});

test("pipelineRecoveryRows surfaces identity conflicts and terminal gate outcomes", () => {
  const rows = pipelineRecoveryRows(
    {
      exists: true,
      stage: "review_rejected",
      identity_mismatches: ["parent2_v"],
      gate_outcome: {
        schema_version: 1,
        kind: "terminal",
        gate_name: "review",
        terminal_stage: "review_rejected",
        reason_code: "strategy_reject",
        failure_class: "strategy",
        disposition: "abandon_generation",
        receipt_digest: ID64,
      },
    },
    {
      component: "reviewer_llm",
      code: "reviewer_llm_unavailable",
      action: "retry_same_tool",
      attempt: 2,
      max_attempts: 3,
      owner_tool: "run_review",
      resume_stage: "quality_passed",
    },
  );
  const classes = rows.map((r) => r.failureClass).sort();
  assert.ok(classes.includes("terminal_gate"));
  assert.ok(classes.includes("checkpoint_epoch_incompatible"));
  assert.ok(classes.includes("infrastructure"));
  const infra = rows.find((r) => r.failureClass === "infrastructure");
  assert.equal(infra.disposition, "auto_retry");
  assert.match(infra.dispositionLabel, /第 2\/3 次/);
});

test("operatorSituationView explains a live Master local retry and canonical successor", () => {
  const status = controlStatusFixture({
    active_generation: {
      generation_ordinal: 1,
      canonical_version: 143,
      canonical_bot_name: "national_v143",
      canonical_tag: "national-bot-v143",
      next_v: 143,
      source_v: 142,
      parent2_v: null,
      stage: "direction_audited",
      run_id: "143#0",
      workflow_run_id: "generation:143:workflow-v68",
      checkpoint_revision: 5,
      attempt: { generation: 0, audit: 0, precommit: 0 },
    },
    stability_observation: {
      last_reset_reason: "generation_abandoned",
      last_reset_details: { abandoned_v: 143, workflow_run_id: "generation:143:workflow-v67" },
    },
  });
  const health = controlHealthFixture(status, {
    stage: "direction_audited",
    checkpoint_revision: 5,
    route: {
      stage: "direction_audited",
      next_v: 143,
      source_v: 142,
      parent2_v: null,
      next_tool: "run_master",
      allowed_tools: ["run_master"],
      intent: "infra_retry",
      action: "retry_same_tool",
      failure_class: "infrastructure",
      directive: "Retry run_master",
      infra_failure: {
        component: "master_llm",
        attempt: 1,
        max_attempts: 3,
        action: "retry_same_tool",
        issues: ["proposal scout: LLM stall timeout after 240.0s"],
      },
    },
  });
  const view = operatorSituationView(status, health);
  assert.match(view.headline, /Master.*局部重试/);
  assert.match(view.why, /240/);
  assert.match(view.next, /2\/3/);
  assert.equal(view.manualRequired, false);
  assert.match(view.continuityNote, /workflow-v67/);
  assert.match(view.continuityNote, /workflow-v68/);
  assert.match(view.continuityNote, /没有被算作已发布 Bot/);
});

test("operatorSituationView describes a checkpoint boundary as completed and names the next tool", () => {
  const status = controlStatusFixture({
    active_generation: {
      generation_ordinal: 2,
      canonical_version: 147,
      canonical_bot_name: "national_v147",
      canonical_tag: "national-bot-v147",
      next_v: 147,
      source_v: 143,
      parent2_v: null,
      stage: "direction_audited",
      run_id: "147#0",
      workflow_run_id: "generation:147:workflow-v1",
      checkpoint_revision: 5,
      attempt: { generation: 0, audit: 0, precommit: 0 },
    },
  });
  const view = operatorSituationView(status, controlHealthFixture(status, {
    stage: "direction_audited",
    route: {
      stage: "direction_audited", next_v: 147, source_v: 143, parent2_v: null,
      next_tool: "run_master", allowed_tools: ["run_master"], intent: "advance", directive: "Continue",
    },
  }));
  assert.match(view.headline, /已完成.*方向审核.*边界/);
  assert.match(view.what, /写入 checkpoint/);
  assert.match(view.next, /Master/);
});

test("operatorSituationView does not call a different canonical generation a same-generation successor", () => {
  const status = controlStatusFixture({
    active_generation: {
      generation_ordinal: 2,
      canonical_version: 144,
      canonical_bot_name: "national_v144",
      canonical_tag: "national-bot-v144",
      next_v: 144,
      source_v: 143,
      parent2_v: null,
      stage: "selected",
      run_id: "144#0",
      workflow_run_id: "generation:144:workflow-v2",
      checkpoint_revision: 1,
      attempt: { generation: 0, audit: 0, precommit: 0 },
    },
    stability_observation: {
      last_reset_reason: "generation_abandoned",
      last_reset_details: { abandoned_v: 143, workflow_run_id: "generation:143:workflow-v68" },
    },
  });
  const view = operatorSituationView(status, controlHealthFixture(status, {
    stage: "selected",
    route: {
      stage: "selected", next_v: 144, source_v: 143, parent2_v: null,
      next_tool: "run_direction_audit", allowed_tools: ["run_direction_audit"],
      intent: "advance", directive: "Continue",
    },
  }));
  assert.equal(view.continuityNote, null);
});

test("operatorSituationView makes first-strict operator certification explicit and zero-strength", () => {
  const active = {
    generation_ordinal: 1,
    canonical_version: 143,
    canonical_bot_name: "national_v143",
    canonical_tag: "national-bot-v143",
    next_v: 143,
    source_v: 142,
    parent2_v: null,
    stage: "official_bootstrap_required",
    run_id: "143#0",
    workflow_run_id: "generation:143:workflow-v70",
    checkpoint_revision: 40,
    attempt: { generation: 0, audit: 0, precommit: 0 },
  };
  const required = controlStatusFixture({
    active_generation: active,
    operator_action: "run_first_strict_official_certification",
  });
  const requiredView = operatorSituationView(required, controlHealthFixture(required, {
    stage: active.stage,
    route: null,
  }));
  assert.equal(requiredView.manualRequired, true);
  assert.match(requiredView.next, /认证/);
  assert.match(requiredView.why, /强度.*权重.*0/);

  const running = controlStatusFixture({
    active_generation: active,
    operator_transition: {
      kind: "first-strict-official-operator-transition",
      state: "bootstrap_running",
      certification_profile: "first_strict_control_v1",
      opponent_authority: "system_control",
      strength_evidence_weight: 0,
      strategy_evidence_weight: 0,
      workflow_run_id: active.workflow_run_id,
      candidate_version: 143,
      source_v: 142,
      checkpoint_stage: active.stage,
      checkpoint_revision: active.checkpoint_revision,
      transition_digest: "a".repeat(64),
    },
  });
  const runningView = operatorSituationView(running, controlHealthFixture(running, {
    stage: active.stage,
    route: null,
  }));
  assert.equal(runningView.manualRequired, false);
  assert.match(runningView.why, /强度.*权重.*0/);

  const ready = {
    ...running,
    operator_transition: { ...running.operator_transition, state: "ready_to_finalize" },
  };
  const readyView = operatorSituationView(ready, controlHealthFixture(ready, {
    stage: active.stage,
    route: null,
  }));
  assert.equal(readyView.manualRequired, true);
  assert.match(readyView.headline, /证书已验证/);
  assert.match(readyView.next, /发布/);
});

test("operatorSituationView treats timeout stages as recovery leases", () => {
  for (const [stage, nextTool] of [["timed_out", "abandon_generation"], ["infra_timed_out", "run_precommit_eval"]]) {
    const status = controlStatusFixture({
      active_generation: {
        generation_ordinal: 1,
        canonical_version: 143,
        canonical_bot_name: "national_v143",
        canonical_tag: "national-bot-v143",
        next_v: 143,
        source_v: 142,
        parent2_v: null,
        stage,
        run_id: "143#0",
        workflow_run_id: "generation:143:workflow-timeout",
        checkpoint_revision: 9,
        attempt: { generation: 0, audit: 0, precommit: 0 },
      },
    });
    const health = controlHealthFixture(status, {
      stage,
      route: {
        stage, next_v: 143, source_v: 142, parent2_v: null,
        next_tool: nextTool, allowed_tools: [nextTool], intent: "recovery", directive: "recover",
      },
    });
    const view = operatorSituationView(status, health);
    assert.match(view.what, /不是成功进度/);
    assert.equal(view.manualRequired, false);
  }
});

function controlStatusFixture(overrides = {}) {
  return {
    running: true,
    epoch_initialized: true,
    operator_action: null,
    operator_command: null,
    active_generation: null,
    post_publication_handoff: { status: "none", blocked: false, issues: [] },
    stability_observation: { last_reset_reason: null },
    ...overrides,
  };
}

function controlHealthFixture(status, pipelineOverrides = {}) {
  return {
    overall: "healthy",
    issues: [],
    running: status.running,
    status,
    active_generation: status.active_generation,
    task: { present: true, done: false, shutdown_requested: false },
    daemon: { alive: true },
    pipeline: {
      exists: true,
      blocked: false,
      recovery: { recoverable: true, issues: [] },
      ...pipelineOverrides,
    },
  };
}
