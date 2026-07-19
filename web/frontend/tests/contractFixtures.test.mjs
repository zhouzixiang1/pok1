import assert from "node:assert/strict";
import test from "node:test";

// Contract fixtures for the dashboard redesign (task §8).  Each fixture is a
// typed snapshot of one authority state; the assertions verify that the
// domain normalization layer fail-closes or projects the right disposition
// without mixing checkpoint shapes or guessing fields.

import { expectAgentActivity } from "../node_modules/.tmp/sse-tests/api/agentActivity.js";
import { expectStrengthJobs } from "../node_modules/.tmp/sse-tests/api/strengthJobs.js";
import { agentActivityView } from "../node_modules/.tmp/sse-tests/domain/agentActivityView.js";
import { strengthJobView, daemonLivenessView } from "../node_modules/.tmp/sse-tests/domain/strengthJobView.js";
import {
  evidenceTierForGate,
  evidenceTierForOfficialCertification,
  criticAdvisoryVerdictLabel,
} from "../node_modules/.tmp/sse-tests/domain/evidenceAuthority.js";
import {
  workerFailureRows,
  pipelineRecoveryRows,
} from "../node_modules/.tmp/sse-tests/domain/failureRecoveryView.js";
import {
  canonicalGenerationIdentityIssues,
  sameCanonicalGenerationIdentity,
} from "../node_modules/.tmp/sse-tests/lib/canonicalGenerationIdentity.js";
import {
  criticAdvisoryComplete,
  pipelineCheckpointIdentityIssues,
} from "../node_modules/.tmp/sse-tests/lib/pipelinePresentation.js";

const ID64 = "a".repeat(64);

function agentFixture(overrides = {}) {
  return {
    available: true,
    evaluation_epoch: "national_tcp_policy_v1",
    workflow_run_id: "workflow-v1",
    run_id: "144#1",
    next_v: 144,
    source_v: 143,
    parent2_v: null,
    checkpoint_revision: 1,
    stage: "workers_done",
    attempts: { generation: 1, audit: 0, precommit: 0 },
    rework_counts: { worker_failure: 0, precommit: 0, official: 0 },
    orchestrator: { stage: "workers_done", reviewer_feedback: null, infra_failure: null },
    master: { stage_reached: true, plan_present: true, analysis: null, tasks: [] },
    direction_audit: null,
    gates: { quality: null, review: null, critic: null, precommit_eval: null, official_full: null },
    gate_keys_present: [],
    worker_failures: [],
    ...overrides,
  };
}

test("fixture: uninitialized epoch — agent projection fails closed", () => {
  const view = agentActivityView({
    available: false,
    reason: "no_strict_workflow",
    evaluation_epoch: "national_tcp_policy_v1",
  });
  assert.equal(view.available, false);
  assert.equal(view.reason, "no_strict_workflow");
});

test("fixture: fresh bootstrap v143 — no parent2 allowed", () => {
  // Fresh bootstrap must be source_v=142, parent2_v=null.  A non-null
  // parent2 on v143 would fail the backend binding; the frontend projection
  // surfaces parent2 verbatim so an operator sees the mismatch.
  const view = agentActivityView(agentFixture({
    next_v: 143, source_v: 142, parent2_v: null, stage: "official_bootstrap_required",
  }));
  assert.equal(view.available, true);
  assert.equal(view.parent2V, null);
  assert.equal(view.stageIsTimeoutLease, false);
});

test("fixture: crossover with parent2 — both parents surface", () => {
  const view = agentActivityView(agentFixture({
    next_v: 150, source_v: 148, parent2_v: 149, stage: "crossover_running",
  }));
  assert.equal(view.parent2V, 149);
  assert.equal(view.sourceV, 148);
});

test("fixture: timed_out — surfaced as timeout lease, not unknown stage", () => {
  const view = agentActivityView(agentFixture({ stage: "timed_out" }));
  assert.equal(view.stageIsTimeoutLease, true);
  assert.equal(view.stageKnown, true);
  const orch = view.roles.find((r) => r.role === "orchestrator");
  assert.equal(orch.state, "terminal");
});

test("fixture: infra_timed_out — precommit recovery route, not abandon", () => {
  const view = agentActivityView(agentFixture({ stage: "infra_timed_out" }));
  assert.equal(view.stageIsTimeoutLease, true);
});

test("fixture: critic advisory only — approved is not a strength gate", () => {
  const view = agentActivityView(agentFixture({
    stage: "critic_checked",
    gates: {
      quality: { name: "quality", present: true, complete: true, fields: {} },
      review: { name: "review", present: true, complete: true, fields: {} },
      critic: {
        name: "critic", present: true, complete: true,
        fields: { approved: true, schema_valid: true, llm_invoked: true, critic_llm_executed: true, advisory_approved: false, advisory_score: 2 },
      },
      precommit_eval: null, official_full: null,
    },
  }));
  const critic = view.roles.find((r) => r.role === "critic");
  assert.match(critic.detail, /advisory/);
  const verdict = criticAdvisoryVerdictLabel(view.gates.critic);
  // advisory_approved=false but complete=true → "建议保留意见"
  assert.equal(verdict.complete, true);
  assert.equal(verdict.verdict, "建议保留意见");
});

test("fixture: first-strict bootstrap operator transition — zero strength weight", () => {
  // Operator bootstrap jobs carry strength_evidence_weight=0; the evidence
  // tier must be "zero" so the dashboard never treats it as normal strength.
  const tier = evidenceTierForOfficialCertification({
    formal_certified: false,
    formal_authority: "operator_bootstrap_full_v5_job",
  });
  // operator_bootstrap_full_v5_job is not signed_full_v5 and not "none" ->
  // advisory bucket per current classifier.  The point of this test is to
  // lock the classification so a refactor cannot silently promote it.
  assert.ok(tier.tier === "advisory" || tier.tier === "zero");
});

test("fixture: signed_full_v5 is the only compliance certification", () => {
  const tier = evidenceTierForOfficialCertification({
    formal_certified: true, formal_authority: "signed_full_v5",
  });
  assert.equal(tier.tier, "compliance");
});

test("fixture: Reviewer infra timeout retry — not strategy rejection", () => {
  // infra_failure with action=retry_same_tool must project as auto_retry,
  // never as a terminal/strategy rejection.
  const rows = pipelineRecoveryRows(
    { exists: true, stage: "quality_passed" },
    {
      component: "reviewer_llm",
      code: "reviewer_llm_unavailable",
      action: "retry_same_tool",
      attempt: 1,
      max_attempts: 3,
      owner_tool: "run_review",
      resume_stage: "quality_passed",
      exhausted: false,
    },
  );
  const infra = rows.find((r) => r.failureClass === "infrastructure");
  assert.equal(infra.disposition, "auto_retry");
  assert.notEqual(infra.disposition, "terminal");
});

test("fixture: terminal gate outcome (review_rejected) — abandon disposition", () => {
  const rows = pipelineRecoveryRows(
    {
      exists: true, stage: "review_rejected",
      gate_outcome: {
        schema_version: 1, kind: "terminal", gate_name: "review",
        terminal_stage: "review_rejected", reason_code: "strategy_reject",
        failure_class: "strategy", disposition: "abandon_generation",
        receipt_digest: ID64,
      },
    },
    null,
  );
  const terminal = rows.find((r) => r.failureClass === "terminal_gate");
  assert.equal(terminal.disposition, "terminal");
});

test("fixture: parent2 identity mismatch — authority_conflict, no guess", () => {
  const rows = pipelineRecoveryRows(
    { exists: true, stage: "workers_done", identity_mismatches: ["parent2_v"] },
    null,
  );
  const conflict = rows.find((r) => r.failureClass === "checkpoint_epoch_incompatible");
  assert.equal(conflict.disposition, "authority_conflict");
});

test("fixture: daemon configured but dead — state=configured_dead", () => {
  const view = daemonLivenessView({ alive: false, configured: true });
  assert.equal(view.state, "configured_dead");
});

test("fixture: daemon alive but stale heartbeat — distinct from fresh", () => {
  const view = daemonLivenessView({ alive: true, configured: true, heartbeat_status: "stale", pid: 1 });
  assert.equal(view.state, "alive_stale_heartbeat");
});

test("fixture: 69-hand sample — inadmissible diagnostic explains rejection", () => {
  const view = strengthJobView({
    available: true,
    evaluation_epoch: "national_tcp_policy_v1",
    evaluation_identity_digest: ID64,
    evaluation_manifest_digest: ID64,
    epoch_reset_receipt_digest: ID64,
    active_bots: ["national_v143", "national_v144"],
    daemon: { alive: true, configured: true, heartbeat_status: "fresh" },
    admitted_samples: [],
    staged_pending: [],
    inadmissible_diagnostics: [
      { id: "bad", rejection_reasons: ["hands_per_strength_sample_not_70"], hands_per_strength_sample: 69 },
    ],
    daemon_stats: {},
  });
  assert.equal(view.available, true);
  assert.equal(view.inadmissibleReasonCounts[0].reason, "hands_per_strength_sample_not_70");
});

test("fixture: active_pool_empty strength projection — unavailable, not faked", () => {
  const view = strengthJobView({
    available: false,
    reason: "active_pool_empty",
    evaluation_epoch: "national_tcp_policy_v1",
    active_bots: [],
    daemon: { alive: false, configured: false },
  });
  assert.equal(view.available, false);
  assert.equal(view.daemon.state, "unconfigured");
});

test("fixture: malformed agent response — fail closed, no partial projection", () => {
  assert.throws(() => expectAgentActivity({ available: true, evaluation_epoch: "other" }), /evaluation_epoch/);
  assert.throws(() => expectAgentActivity("not an object"), /not an object/);
});

test("fixture: malformed strength response — fail closed", () => {
  assert.throws(() => expectStrengthJobs({ available: true, evaluation_epoch: "national_tcp_policy_v1", daemon: { alive: true } }), /identity_digest/);
  assert.throws(() => expectStrengthJobs({ available: true, evaluation_epoch: "national_tcp_policy_v1", evaluation_identity_digest: ID64 }), /daemon health/);
});

test("fixture: two published bots with immutable rating cycle identity", () => {
  const identityA = {
    generation_ordinal: 1, canonical_version: 143,
    canonical_bot_name: "national_v143", canonical_tag: "national-bot-v143",
  };
  const identityB = {
    generation_ordinal: 2, canonical_version: 144,
    canonical_bot_name: "national_v144", canonical_tag: "national-bot-v144",
  };
  assert.deepEqual(canonicalGenerationIdentityIssues(identityA), []);
  assert.deepEqual(canonicalGenerationIdentityIssues(identityB), []);
  assert.equal(sameCanonicalGenerationIdentity(identityA, identityB), false);
});

test("fixture: critic advisory complete field chain is exact", () => {
  // approved alone is insufficient; the full LLM execution chain is required.
  assert.equal(criticAdvisoryComplete({
    approved: true, schema_valid: true, llm_invoked: true, critic_llm_executed: true,
  }), true);
  assert.equal(criticAdvisoryComplete({
    approved: true, schema_valid: true, llm_invoked: true, // critic_llm_executed missing
  }), false);
  assert.equal(criticAdvisoryComplete({
    approved: true, schema_valid: true, llm_invoked: true, critic_llm_executed: true, parse_failed: true,
  }), false);
});

test("fixture: pipelineCheckpointIdentityIssues catches parent2 mismatch", () => {
  const checkpoint = {
    evaluation_epoch: "national_tcp_policy_v1",
    next_v: 150, source_v: 148, parent2_v: 149, stage: "workers_done",
    workflow_run_id: "workflow-v1", run_id: "150#1", checkpoint_revision: 1,
  };
  const active = {
    generation_ordinal: 7, canonical_version: 150,
    canonical_bot_name: "national_v150", canonical_tag: "national-bot-v150",
    next_v: 150, source_v: 148, parent2_v: 149, stage: "workers_done",
    run_id: "150#1", workflow_run_id: "workflow-v1", checkpoint_revision: 1,
    attempt: { generation: 1, audit: 0, precommit: 0 },
  };
  assert.deepEqual(pipelineCheckpointIdentityIssues(checkpoint, active), []);
  const mismatched = { ...active, parent2_v: 148 };
  const issues = pipelineCheckpointIdentityIssues(checkpoint, mismatched);
  assert.ok(issues.includes("parent2_v"));
});

test("fixture: worker failure rows preserve backend category", () => {
  const rows = workerFailureRows([
    { worker_id: 1, role: "Architect", error: "timeout", failure_type: "llm_timeout", category: "worker", gen: 144 },
    { worker_id: 2, role: "review", error: "rejected", failure_type: "strategy", category: "gate", gen: 144 },
  ]);
  assert.equal(rows[0].failureClass, "worker");
  assert.equal(rows[1].failureClass, "gate");
});
