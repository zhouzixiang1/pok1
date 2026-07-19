import assert from "node:assert/strict";
import test from "node:test";

import { expectAgentActivity } from "../node_modules/.tmp/sse-tests/api/agentActivity.js";
import { expectStrengthJobs } from "../node_modules/.tmp/sse-tests/api/strengthJobs.js";
import {
  agentActivityView,
  agentRoleSummaries,
} from "../node_modules/.tmp/sse-tests/domain/agentActivityView.js";
import {
  strengthJobView,
  daemonLivenessView,
  strengthRejectionLabel,
} from "../node_modules/.tmp/sse-tests/domain/strengthJobView.js";
import {
  evidenceTierForGate,
  evidenceTierForOfficialCertification,
  criticAdvisoryVerdictLabel,
  EVIDENCE_TIER_LABELS,
} from "../node_modules/.tmp/sse-tests/domain/evidenceAuthority.js";
import {
  workerFailureRows,
  pipelineRecoveryRows,
} from "../node_modules/.tmp/sse-tests/domain/failureRecoveryView.js";

const ID64 = "a".repeat(64);

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
    orchestrator: { stage: "workers_done", reviewer_feedback: null, infra_failure: null },
    master: { stage_reached: true, plan_present: true, analysis: null, tasks: [] },
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
});

test("agentRoleSummaries reports critic as advisory and not a strength gate", () => {
  const projection = baseProjection({
    stage: "critic_checked",
    gates: {
      quality: { name: "quality", present: true, complete: true, fields: {} },
      review: { name: "review", present: true, complete: true, fields: {} },
      critic: {
        name: "critic",
        present: true,
        complete: true,
        fields: { advisory_approved: false, advisory_score: 3 },
      },
      precommit_eval: null,
      official_full: null,
    },
  });
  const roles = agentRoleSummaries(projection);
  const critic = roles.find((r) => r.role === "critic");
  assert.equal(critic.state, "terminal");
  assert.match(critic.detail, /advisory/);
});

test("agentActivityView flags timed_out as a timeout lease, not a known pipeline stage", () => {
  const view = agentActivityView(baseProjection({ stage: "timed_out" }));
  assert.equal(view.available, true);
  assert.equal(view.stageIsTimeoutLease, true);
  // timed_out is not in PIPELINE_STAGE_CONTRACT but is a known timeout lease.
  assert.equal(view.stageKnown, true);
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
    daemon: { alive: true, configured: true, heartbeat_status: "fresh" },
    admitted_samples: [],
    staged_pending: [],
    inadmissible_diagnostics: [],
    daemon_stats: {},
  });
  assert.equal(ok.available, true);

  assert.throws(
    () => expectStrengthJobs({
      available: true,
      evaluation_epoch: "national_tcp_policy_v1",
      evaluation_identity_digest: "short",
      active_bots: [],
      daemon: { alive: true },
      admitted_samples: [],
      staged_pending: [],
      inadmissible_diagnostics: [],
      daemon_stats: {},
    }),
    /identity_digest is invalid/,
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
    daemon: { alive: true, configured: true, heartbeat_status: "fresh", pid: 123, heartbeat_age_sec: 5 },
    admitted_samples: [{ id: "m1", bot0: "national_v143", bot1: "national_v144" }],
    staged_pending: [],
    inadmissible_diagnostics: [
      { id: "bad1", rejection_reasons: ["hands_per_strength_sample_not_70", "bot1_not_in_active_pool"] },
      { id: "bad2", rejection_reasons: ["hands_per_strength_sample_not_70"] },
    ],
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

test("evidenceTierForGate classifies critic and official_full correctly", () => {
  assert.equal(evidenceTierForGate(null).tier, "zero");
  const critic = evidenceTierForGate({ name: "critic", present: true, complete: true, fields: {} });
  assert.equal(critic.tier, "advisory");
  const officialPassed = evidenceTierForGate({ name: "official_full", present: true, complete: true, fields: {} });
  assert.equal(officialPassed.tier, "compliance");
  const officialFailed = evidenceTierForGate({ name: "official_full", present: true, complete: false, fields: {} });
  assert.equal(officialFailed.tier, "zero");
});

test("evidenceTierForOfficialCertification only treats signed_full_v5 as compliance", () => {
  const signed = evidenceTierForOfficialCertification({ formal_certified: true, formal_authority: "signed_full_v5" });
  assert.equal(signed.tier, "compliance");
  const none = evidenceTierForOfficialCertification({ formal_certified: false, formal_authority: "none" });
  assert.equal(none.tier, "zero");
});

test("criticAdvisoryVerdictLabel mirrors criticAdvisoryComplete field chain", () => {
  const complete = criticAdvisoryVerdictLabel({
    name: "critic",
    present: true,
    complete: true,
    fields: { approved: true, schema_valid: true, llm_invoked: true, critic_llm_executed: true, advisory_approved: true },
  });
  assert.equal(complete.complete, true);
  assert.equal(complete.verdict, "建议支持");

  const incomplete = criticAdvisoryVerdictLabel({
    name: "critic",
    present: true,
    complete: false,
    fields: { approved: true, schema_valid: true }, // missing llm chain
  });
  assert.equal(incomplete.complete, false);
});

test("EVIDENCE_TIER_LABELS exposes all five tiers", () => {
  const tiers = Object.keys(EVIDENCE_TIER_LABELS).sort();
  assert.deepEqual(tiers, ["advisory", "compliance", "diagnostic", "strength", "zero"]);
});

test("workerFailureRows keeps the backend category without re-deriving it", () => {
  const rows = workerFailureRows([
    { worker_id: 1, role: "Tuner", error: "boom", failure_type: "x", category: "worker", gen: 144 },
    { worker_id: 2, role: "Gate", error: "kaboom", failure_type: "y", category: "gate", gen: 144 },
  ]);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].failureClass, "worker");
  assert.equal(rows[1].failureClass, "gate");
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
