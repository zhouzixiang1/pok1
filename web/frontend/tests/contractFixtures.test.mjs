import assert from "node:assert/strict";
import test from "node:test";
import { existsSync, readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

// Resolve the project Web Python interpreter for the cross-language fixture
// tests. An explicit ``PYTHON`` env var always wins; otherwise fall back to the
// repo venv (``<repo>/.venv/bin/python``) so the contract fixtures run from a
// normal checkout without forcing the operator to export ``PYTHON``.
function resolveProjectPython() {
  const here = dirname(fileURLToPath(import.meta.url));
  const candidates = [
    resolve(here, "..", "..", "..", ".venv", "bin", "python"),
    resolve(here, "..", "..", ".venv", "bin", "python"),
  ];
  return candidates.find((candidate) => existsSync(candidate)) ?? null;
}

// Contract fixtures for the dashboard redesign (task §8).  Each fixture is a
// typed snapshot of one authority state; the assertions verify that the
// domain normalization layer fail-closes or projects the right disposition
// without mixing checkpoint shapes or guessing fields.

import { expectAgentActivity, agentActivityBindingIssues, agentWorkflowIdentityKey } from "../node_modules/.tmp/sse-tests/api/agentActivity.js";
import { expectStrengthJobs, strengthJobsBindingIssues } from "../node_modules/.tmp/sse-tests/api/strengthJobs.js";
import { agentActivityView } from "../node_modules/.tmp/sse-tests/domain/agentActivityView.js";
import { strengthJobView, daemonLivenessView } from "../node_modules/.tmp/sse-tests/domain/strengthJobView.js";
import {
  evidenceTierForGate,
  criticAdvisoryVerdictLabel,
} from "../node_modules/.tmp/sse-tests/domain/evidenceAuthority.js";
import {
  workerFailureRows,
  pipelineRecoveryRows,
} from "../node_modules/.tmp/sse-tests/domain/failureRecoveryView.js";
import { operatorSituationView } from "../node_modules/.tmp/sse-tests/domain/operatorSituationView.js";
import {
  FIRST_STRICT_POLICY_VERSION,
  canonicalGenerationIdentityIssues,
  sameCanonicalGenerationIdentity,
} from "../node_modules/.tmp/sse-tests/lib/canonicalGenerationIdentity.js";
import {
  criticAdvisoryComplete,
  pipelineCheckpointIdentityIssues,
} from "../node_modules/.tmp/sse-tests/lib/pipelinePresentation.js";

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

// Branch-portable inline-fixture bot/tag names. These are validator-structure
// test data (the TS validators accept either the national_v or national_cloud_v
// namespace per canonicalGenerationIdentity), but per AGENTS.md we still derive
// them from FIRST_STRICT_POLICY_VERSION instead of hardcoding a main-branch
// version literal. The namespace prefix is the cloud line's active prefix.
const STRICT_V = FIRST_STRICT_POLICY_VERSION;
const NEXT_V = FIRST_STRICT_POLICY_VERSION + 1;
const STRICT_BOT = `national_cloud_v${STRICT_V}`;
const NEXT_BOT = `national_cloud_v${NEXT_V}`;
const STRICT_TAG = `national-cloud-bot-v${STRICT_V}`;
const NEXT_TAG = `national-cloud-bot-v${NEXT_V}`;
const strengthAuthority = (activeBots = ["national_v143", "national_v144"]) => ({
  evaluation_epoch: "national_tcp_policy_v1",
  active_bots: activeBots,
  epoch_reset_receipt_digest: ID64,
  evaluation_identity_digest: ID64,
  evaluation_manifest_digest: ID64,
  complete: true,
});
const EXPECTED_AGENT_GATE_FIELDS = {
  quality: ["all_passed", "code_fingerprint", "critical_scenarios_passed", "decision_pass_rate", "workflow_profile_digest"],
  review: ["approved", "llm_failed", "llm_invoked", "parse_failed", "quality_score", "receipt_digest", "reviewer_llm_executed", "schema_valid"],
  critic: ["advisory_approved", "advisory_score", "approved", "critic_llm_executed", "llm_failed", "llm_invoked", "parse_failed", "receipt_digest", "schema_valid"],
  precommit_eval: ["attempt", "candidate_artifact_hash", "hands_per_match", "native_matches", "passed", "receipt_digest"],
};
const page = (admitted = 0, staged = 0, inadmissible = 0) => ({
  offset: 0, limit: 50,
  admitted_total: admitted, staged_pending_total: staged, inadmissible_total: inadmissible,
  admitted_has_more: false, staged_pending_has_more: false, inadmissible_has_more: false,
});

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
    master: { started: true, completed: true, plan_present: true, analysis: null, tasks: [], task_total: 0, tasks_truncated: false },
    direction_audit: null,
    gates: { quality: null, review: null, critic: null, precommit_eval: null },
    gate_keys_present: [],
    worker_failures: [],
    worker_failures_truncated: false,
    observer_limits: { max_tasks: 8, max_target_files_per_task: 8, max_worker_failures: 10, max_response_bytes: 64 * 1024 },
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

test("fixture: real Python dashboard builders satisfy frontend validators", () => {
  const python = process.env.PYTHON || resolveProjectPython();
  assert.ok(
    python,
    "set PYTHON to the project Web interpreter before npm test, or run from a checkout with /.venv/bin/python",
  );
  const result = spawnSync(
    python,
    ["tests/captureDashboardAuthority.py"],
    { cwd: process.cwd(), encoding: "utf8", timeout: 30_000 },
  );
  assert.equal(result.status, 0, result.error?.message || result.stderr || result.stdout);
  const captured = JSON.parse(result.stdout);
  const agents = expectAgentActivity(captured.agents);
  const strength = expectStrengthJobs(captured.strength);
  assert.equal(agents.available, true);
  assert.equal(agents.worker_failures[0].record_state, "historical");
  assert.equal(agents.worker_failures[0].current_blocker, false);
  assert.deepEqual(Object.keys(agents.orchestrator.infra_failure).sort(), [
    "action", "attempt", "code", "component", "exhausted", "failure_class",
    "identity_digest", "max_attempts", "operation", "owner_tool", "reason",
    "resume_stage", "retryable", "schema_version",
  ]);
  for (const [name, expectedFields] of Object.entries(EXPECTED_AGENT_GATE_FIELDS)) {
    assert.deepEqual(Object.keys(agents.gates[name].fields).sort(), expectedFields);
  }
  assert.equal(strength.available, false);
  assert.equal(strength.authority_binding.complete, true);
  // The fixture echoes a branch-portable strict-generation identity; assert the
  // emitted active_bots match the canonical active-namespace bot name (derived
  // via bot_name(FIRST_STRICT_POLICY_VERSION)) rather than a hardcoded literal.
  assert.deepEqual(strength.active_bots, [captured.strict_identity.strict_bot_name]);
  assert.equal(
    captured.strict_identity.first_strict_policy_version,
    FIRST_STRICT_POLICY_VERSION,
    "fixture first-strict floor must match the TS FIRST_STRICT_POLICY_VERSION",
  );
  assert.equal(strength.capabilities.producer_consumer_dispatch, false);
});

test("fixture: fresh bootstrap v143 — no parent2 allowed", () => {
  // Fresh bootstrap must be source_v=142, parent2_v=null.  A non-null
  // parent2 on v143 would fail the backend binding; the frontend projection
  // surfaces parent2 verbatim so an operator sees the mismatch.
  const view = agentActivityView(agentFixture({
    next_v: 143, source_v: 142, parent2_v: null, stage: "verified",
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
      quality: { name: "quality", present: true, complete: true, authority_state: "current", fields: {} },
      review: { name: "review", present: true, complete: true, authority_state: "current", fields: {} },
      critic: {
        name: "critic", present: true, complete: true, authority_state: "current",
        fields: { approved: true, schema_valid: true, llm_invoked: true, critic_llm_executed: true, advisory_approved: false, advisory_score: 2 },
      },
      precommit_eval: null,
    },
  }));
  const critic = view.roles.find((r) => r.role === "critic");
  assert.match(critic.detail, /建议.*不单独决定/);
  const verdict = criticAdvisoryVerdictLabel(view.gates.critic);
  // advisory_approved=false but complete=true → "建议保留意见"
  assert.equal(verdict.complete, true);
  assert.equal(verdict.verdict, "建议保留意见");
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

test("fixture: daemon configured=false but alive is an explicit red conflict", () => {
  const view = daemonLivenessView({ alive: true, configured: false, pid: 42, heartbeat_status: "fresh" });
  assert.equal(view.state, "configuration_conflict");
  assert.match(view.detail, /配置明确禁用.*存活进程/);
});

test("fixture: 69-hand sample — inadmissible diagnostic explains rejection", () => {
  const view = strengthJobView({
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
    inadmissible_diagnostics: [
      { id: "bad", rejection_reasons: ["hands_per_strength_sample_not_70"], hands_per_strength_sample: 69 },
    ],
    pagination: page(0, 0, 1),
    observer: OBSERVER,
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
    epoch_reset_receipt_digest: ID64,
    capabilities: STRENGTH_CAPABILITIES,
    authority_binding: {
      ...strengthAuthority([]),
      evaluation_identity_digest: null,
      evaluation_manifest_digest: null,
    },
    daemon: { alive: false, configured: false },
  });
  assert.equal(view.available, false);
  assert.equal(view.daemon.state, "unconfigured");
});

test("fixture: strength observation is hidden after active-pool or reset rotation", () => {
  const observed = {
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
  assert.deepEqual(strengthJobsBindingIssues(observed, {
    active_bots: ["national_v143", "national_v144"],
    reset_receipt_digest: ID64,
  }), []);
  assert.ok(strengthJobsBindingIssues(observed, {
    active_bots: ["national_v143"],
    reset_receipt_digest: ID64,
  }).includes("active_bots"));
  assert.ok(strengthJobsBindingIssues(observed, {
    active_bots: ["national_v143", "national_v144"],
    reset_receipt_digest: "b".repeat(64),
  }).includes("epoch_reset_receipt_digest"));
});

test("fixture: malformed agent response — fail closed, no partial projection", () => {
  assert.throws(() => expectAgentActivity({ available: true, evaluation_epoch: "other" }), /evaluation_epoch/);
  assert.throws(() => expectAgentActivity("not an object"), /not an object/);
});

test("fixture: nested agent structures fail closed and exact control binding catches every identity field", () => {
  const fixture = agentFixture();
  assert.equal(expectAgentActivity(fixture).available, true);
  assert.throws(() => expectAgentActivity({ ...fixture, master: { started: true } }), /structurally incomplete|nested contract/);
  assert.throws(() => expectAgentActivity({
    ...fixture,
    gates: {
      ...fixture.gates,
      quality: {
        name: "quality", present: true, complete: true, authority_state: "current",
        fields: { all_passed: true, status: { raw_receipt: "x".repeat(100_000) } },
      },
    },
  }), /nested contract/);
  const active = {
    generation_ordinal: 2, canonical_version: 144,
    canonical_bot_name: "national_v144", canonical_tag: "national-bot-v144",
    next_v: 144, source_v: 143, parent2_v: null, stage: "workers_done",
    run_id: "144#1", workflow_run_id: "workflow-v1", checkpoint_revision: 1,
    attempt: { generation: 1, audit: 0, precommit: 0 },
  };
  assert.deepEqual(agentActivityBindingIssues(fixture, active), []);
  for (const [field, changed] of [
    ["next_v", 145], ["source_v", 142], ["parent2_v", 142], ["stage", "quality_passed"],
    ["run_id", "144#2"], ["workflow_run_id", "workflow-v2"], ["checkpoint_revision", 2],
  ]) {
    const issues = agentActivityBindingIssues({ ...fixture, [field]: changed }, active);
    assert.ok(issues.includes(field));
  }
});

test("fixture: malformed strength response — fail closed", () => {
  assert.throws(() => expectStrengthJobs({ available: true, evaluation_epoch: "national_tcp_policy_v1", daemon: { alive: true } }), /authority binding/);
  assert.throws(() => expectStrengthJobs({ available: true, evaluation_epoch: "national_tcp_policy_v1", evaluation_identity_digest: ID64 }), /daemon health/);
});

test("fixture: nested staged evidence fails closed on identity drift", () => {
  const badStaged = {
    available: true, evaluation_epoch: "national_tcp_policy_v1",
    evaluation_identity_digest: ID64, evaluation_manifest_digest: ID64,
    epoch_reset_receipt_digest: ID64, active_bots: ["national_v143", "national_v144"],
    capabilities: STRENGTH_CAPABILITIES, authority_binding: strengthAuthority(),
    daemon: { alive: true, configured: true, heartbeat_status: "fresh" },
    admitted_samples: [], inadmissible_diagnostics: [], pagination: page(0, 1, 0), observer: OBSERVER, daemon_stats: {},
    staged_pending: [{
      filename: "old.json", id: "old.json", timestamp: null,
      bot0: "national_v143", bot1: "national_v144",
      evaluation_identity_digest: "b".repeat(64), strength_sample_unit: "70_hand_match",
      hands_per_strength_sample: 70, strength_sample_count: 1,
      strength_admitted: true, strength_complete: true, strength_compliance_passed: true,
    }],
  };
  assert.throws(() => expectStrengthJobs(badStaged), /nested evidence/);
});

test("fixture: repair stages expose old gates as historical-invalidated, never current green", () => {
  const projection = agentFixture({
    stage: "rework_running",
    orchestrator: { stage: "rework_running", reviewer_feedback: null, infra_failure: null },
    gates: {
      quality: { name: "quality", present: true, complete: false, authority_state: "historical_invalidated", fields: { all_passed: true } },
      review: { name: "review", present: true, complete: false, authority_state: "historical_invalidated", fields: { approved: true } },
      critic: null, precommit_eval: null,
    },
  });
  assert.equal(expectAgentActivity(projection).available, true);
  assert.equal(evidenceTierForGate(projection.gates.quality).tier, "zero");
  assert.match(evidenceTierForGate(projection.gates.quality).label, /已失效/);
  const view = agentActivityView(projection);
  assert.equal(view.roles.find((role) => role.role === "workers").state, "running");
  assert.match(view.roles.find((role) => role.role === "reviewer").detail, /历史诊断/);
  assert.throws(() => expectAgentActivity({
    ...projection,
    gates: {
      ...projection.gates,
      quality: { ...projection.gates.quality, complete: true },
    },
  }), /nested contract/);
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

test("fixture: real workflow-v68 Master retry is explained as automatic local recovery", () => {
  const status = {
    running: true,
    epoch_initialized: true,
    operator_action: null,
    operator_command: null,
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
    post_publication_handoff: { status: "none", blocked: false, issues: [] },
    stability_observation: {
      last_reset_reason: "generation_abandoned",
      last_reset_details: {
        abandoned_v: 143,
        reason: "system_strict_authority_invalid:strict_authority_schema_retry_exhausted:proposal:mechanism",
        workflow_run_id: "generation:143:workflow-v67",
      },
    },
  };
  const health = {
    overall: "healthy",
    issues: [],
    running: true,
    status,
    task: { present: true, done: false, shutdown_requested: false },
    daemon: { configured: true, alive: true, heartbeat_status: "fresh" },
    pipeline: {
      exists: true,
      stage: "direction_audited",
      blocked: false,
      recovery: { active: true, recoverable: true, issues: [] },
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
        directive: "Retry run_master for master_llm infrastructure attempt 2/3.",
        infra_failure: {
          component: "master_llm",
          attempt: 1,
          max_attempts: 3,
          action: "retry_same_tool",
          issues: ["proposal_scout_repair:counterfactual: LLM stall timeout after 240.0s"],
        },
      },
    },
  };
  const view = operatorSituationView(status, health);
  assert.equal(view.manualRequired, false);
  assert.match(view.headline, /Master/);
  assert.match(view.next, /2\/3/);
  assert.match(view.continuityNote, /successor|新尝试/);
});

test("fixture: real v67 to v68 successor changes the Agent Activity hard fence", () => {
  const shared = {
    generation_ordinal: 1, canonical_version: 143,
    canonical_bot_name: "national_v143", canonical_tag: "national-bot-v143",
    next_v: 143, source_v: 142, parent2_v: null,
    stage: "direction_audited", run_id: "143#0", checkpoint_revision: 5,
    attempt: { generation: 0, audit: 0, precommit: 0 },
  };
  const v67 = { ...shared, workflow_run_id: "generation:143:workflow-v67" };
  const v68 = { ...shared, workflow_run_id: "generation:143:workflow-v68" };
  assert.notEqual(agentWorkflowIdentityKey(v67), agentWorkflowIdentityKey(v68));
  assert.notEqual(
    agentWorkflowIdentityKey(v68),
    agentWorkflowIdentityKey({ ...v68, checkpoint_revision: 6 }),
  );
});

test("fixture: dashboard derives producer-consumer copy from backend capability", () => {
  const source = readFileSync(new URL("../src/pages/BackgroundStrength.tsx", import.meta.url), "utf8");
  assert.match(source, /producerConsumerCapabilityView/);
  assert.doesNotMatch(source, /inert shadow/);
  assert.match(source, /只有绑定当前 reset、发布池与评测身份/);
});

test("fixture: completed Reviewer is a binding compliance gate while Critic remains advisory", () => {
  const review = evidenceTierForGate({ name: "review", present: true, complete: true, authority_state: "current", fields: {} });
  const critic = evidenceTierForGate({ name: "critic", present: true, complete: true, authority_state: "current", fields: {} });
  assert.equal(review.tier, "compliance");
  assert.equal(critic.tier, "advisory");
});

test("fixture: incomplete Critic has zero authority and direction-audited Master retry stays running", () => {
  const incompleteCritic = evidenceTierForGate({ name: "critic", present: true, complete: false, authority_state: "current", fields: {} });
  assert.equal(incompleteCritic.tier, "zero");
  const response = agentFixture({
    stage: "direction_audited",
    master: { started: true, completed: false, plan_present: false, analysis: null, tasks: [], task_total: 0, tasks_truncated: false },
  });
  const view = agentActivityView(response, {
    stage: "direction_audited", next_v: 144, source_v: 143, parent2_v: null,
    next_tool: "run_master", allowed_tools: ["run_master"], intent: "infra_retry",
    action: "retry_same_tool", failure_class: "infrastructure", directive: "retry",
    infra_failure: { component: "master_llm", attempt: 1, max_attempts: 3 },
  });
  const master = view.roles.find((row) => row.role === "master");
  assert.equal(master.state, "running");
  assert.match(master.detail, /重试/);
});
