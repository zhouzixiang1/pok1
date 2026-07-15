import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";

import {
  createDataStreamController,
  createInitialDataStore,
  validateDataStreamEvent,
} from "../node_modules/.tmp/sse-tests/lib/dataStreamController.js";
import {
  createEvolutionStreamController,
  validateEvolutionStreamEvent,
} from "../node_modules/.tmp/sse-tests/lib/evolutionStreamController.js";
import {
  epochStreamAuthorityKey,
} from "../node_modules/.tmp/sse-tests/lib/epochStreamAuthority.js";
import {
  controlTaskActive,
  controlTaskStopping,
} from "../node_modules/.tmp/sse-tests/lib/controlRuntimeState.js";
import {
  criticAdvisoryComplete,
  criticAdvisoryVerdict,
  pipelineCheckpointIdentityIssues,
} from "../node_modules/.tmp/sse-tests/lib/pipelinePresentation.js";
import {
  expectPipelineCheckpoint,
} from "../node_modules/.tmp/sse-tests/api/pipeline.js";

test("Critic presentation uses the advisory verdict, not execution completion", () => {
  const negativeAdvice = {
    approved: true,
    advisory_approved: false,
    schema_valid: true,
    llm_invoked: true,
    critic_llm_executed: true,
    llm_failed: false,
    parse_failed: false,
  };

  assert.equal(criticAdvisoryComplete(negativeAdvice), true);
  assert.equal(criticAdvisoryVerdict(negativeAdvice), "建议保留意见");
  assert.equal(criticAdvisoryVerdict({ ...negativeAdvice, advisory_approved: true }), "建议支持");
  assert.equal(criticAdvisoryVerdict({ ...negativeAdvice, advisory_approved: undefined }), "建议结论不可用");
});

test("checkpoint API and presentation reject a stale same-stage revision", () => {
  const checkpoint = expectPipelineCheckpoint({
    checkpoint_schema_version: 2,
    evaluation_epoch: "national_tcp_policy_v1",
    checkpoint_revision: 7,
    next_v: 143,
    source_v: 142,
    stage: "reviewed",
    workflow_run_id: "workflow-v1",
    run_id: "143#1",
  });
  assert.ok(checkpoint);
  const active = {
    next_v: 143,
    source_v: 142,
    stage: "reviewed",
    workflow_run_id: "workflow-v1",
    run_id: "143#1",
    checkpoint_revision: 8,
    attempt: { generation: 1, audit: 0, precommit: 0 },
  };

  assert.deepEqual(pipelineCheckpointIdentityIssues(checkpoint, active), ["checkpoint_revision"]);
  assert.deepEqual(
    pipelineCheckpointIdentityIssues({ ...checkpoint, checkpoint_revision: 8 }, active),
    [],
  );
  assert.throws(
    () => expectPipelineCheckpoint({ ...checkpoint, checkpoint_revision: undefined }),
    /pipeline checkpoint is structurally incomplete/,
  );
});

test("unfinished control task retains ownership through cancel and shutdown requests", () => {
  const fixture = {
    present: true,
    done: false,
    cancelled: false,
    shutdown_requested: false,
  };
  assert.equal(controlTaskActive(fixture), true);
  assert.equal(controlTaskStopping(fixture), false);
  assert.equal(controlTaskActive({ ...fixture, cancelled: true }), true);
  assert.equal(controlTaskActive({ ...fixture, shutdown_requested: true }), true);
  assert.equal(controlTaskStopping({ ...fixture, shutdown_requested: true }), true);
  assert.equal(controlTaskActive({ ...fixture, done: true }), false);
  assert.equal(controlTaskActive({ ...fixture, present: false }), false);
});

class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.onopen = null;
    this.onerror = null;
    this.closed = false;
    this.closeCount = 0;
    this.listeners = new Map();
  }

  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close() {
    this.closed = true;
    this.closeCount += 1;
  }

  open() {
    this.onopen?.();
  }

  error() {
    this.onerror?.();
  }

  emit(type, value) {
    this.emitRaw(type, JSON.stringify(value));
  }

  emitRaw(type, data) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data });
    }
  }
}

class FakeEventSourceFactory {
  constructor() {
    this.sources = [];
  }

  create = (url) => {
    const source = new FakeEventSource(url);
    this.sources.push(source);
    return source;
  };
}

class FakeScheduler {
  constructor() {
    this.nowValue = 0;
    this.nextId = 1;
    this.records = new Map();
  }

  setTimeout = (callback, delayMs) => {
    const id = this.nextId++;
    this.records.set(id, { callback, delayMs, active: true });
    return id;
  };

  clearTimeout = (id) => {
    const record = this.records.get(id);
    if (record) record.active = false;
  };

  now = () => this.nowValue;

  pendingIds() {
    return [...this.records.entries()]
      .filter(([, record]) => record.active)
      .map(([id]) => id);
  }

  fire(id, { force = false } = {}) {
    const record = this.records.get(id);
    assert.ok(record, `timer ${id} must exist`);
    if (!record.active && !force) return false;
    record.active = false;
    record.callback();
    return true;
  }

  runNext() {
    const [id] = this.pendingIds();
    assert.ok(id, "one reconnect timer must be pending");
    this.fire(id);
    return id;
  }
}

function rating(name, score) {
  return {
    name,
    rating: score,
    rd: 80,
    sigma: 0.06,
    conservative_rating: score - 160,
    confidence: "confident",
    last_period: "2026-07-15T00:00:00",
  };
}

function daemonStatus() {
  return {
    status: "active",
    last_update_age_seconds: 0,
    daemon_enabled: true,
    daemon_configured: true,
    process_alive: true,
  };
}

function botSummary(name = "national_v143") {
  return {
    name,
    version: Number(name.slice("national_v".length)),
    completed: true,
    total_lines: 100,
    files: ["policy.py"],
    rating: { r: 1500, rd: 80, conservative: 1340 },
    active: true,
    tagged: true,
    reaped: false,
    protocol_eligible: true,
    protocol_errors: [],
    lifecycle_status: "active",
    strength_evidence_available: true,
    strength_evidence_status: "current_evaluation_cycle",
    official_certification: {
      bot: name,
      status: "official-certified",
    },
  };
}

function matchSummary() {
  return {
    id: "match-1",
    timestamp: "20260715_000000_000000",
    execution_mode: "native_tcp",
    evaluation_epoch: "national_tcp_policy_v1",
    evaluation_identity_digest: "c".repeat(64),
    bot0: "national_v143",
    bot1: "national_v144",
    bot0_wins: 1,
    bot1_wins: 0,
    draws: 0,
    strength_sample_unit: "70_hand_match",
    hands_per_strength_sample: 70,
    strength_admitted: true,
    strength_complete: true,
    strength_compliance_passed: true,
    strength_sample_count: 1,
    net_chips_bot0: [100],
  };
}

function generationCostPolicy() {
  return {
    policy_id: "operator-generation-cost-v1",
    enforcement_mode: "monitor_only",
    warning_usd: 1,
    hard_limit_usd: null,
    receipt_sha256: "d".repeat(64),
    configuration_from_llm_input: false,
    same_uid_llm_resistance: false,
    candidate_sandbox_mutable: false,
    workflow_guarded_paths: true,
  };
}

function dataHarness() {
  const factory = new FakeEventSourceFactory();
  const scheduler = new FakeScheduler();
  let store = createInitialDataStore();
  const updateStore = (update) => {
    store = typeof update === "function" ? update(store) : update;
  };
  const controller = createDataStreamController(updateStore, "1".repeat(64), {
    createSource: factory.create,
    scheduler,
  });
  return {
    controller,
    factory,
    scheduler,
    store: () => store,
  };
}

test("data stream open, ping, and valid events update the production store; malformed JSON is not liveness", () => {
  const harness = dataHarness();
  const stop = harness.controller.start();
  const source = harness.factory.sources[0];

  assert.equal(source.url, `/api/data/stream?authority=${"1".repeat(64)}`);
  assert.equal(harness.store().stream.state, "connecting");
  source.open();
  assert.equal(harness.store().stream.state, "connected");
  assert.equal(harness.store().stream.last_event_at, null);

  harness.scheduler.nowValue = 100;
  source.emitRaw("ping", "{");
  assert.equal(harness.store().stream.last_event_at, null);
  source.emit("ping", {});
  assert.equal(harness.store().stream.last_event_at, 100);

  harness.scheduler.nowValue = 200;
  source.emit("daemon", daemonStatus());
  assert.equal(harness.store().daemon.status, "active");
  assert.equal(harness.store().stream.last_event_at, 200);

  harness.scheduler.nowValue = 300;
  source.emitRaw("ratings", "{");
  assert.deepEqual(harness.store().ratings, []);
  assert.equal(harness.store().stream.last_event_at, 200);

  source.emit("ratings", null);
  source.emit("ratings", { name: "object-not-array", rating: 1 });
  assert.deepEqual(harness.store().ratings, []);
  assert.equal(harness.store().stream.last_event_at, 200);

  harness.scheduler.nowValue = 400;
  source.emit("ratings", [rating("national_v143", 1500)]);
  assert.equal(harness.store().ratings[0].name, "national_v143");
  assert.equal(harness.store().stream.last_event_at, 400);
  stop();
});

test("malformed epoch_blocked fences, closes, clears, and never reconnects before parsing", () => {
  const harness = dataHarness();
  harness.controller.start();
  const source = harness.factory.sources[0];
  source.open();
  source.emit("ratings", [rating("stale", 9999)]);

  source.emitRaw("epoch_blocked", "{");

  assert.equal(source.closed, true);
  assert.deepEqual(harness.store().ratings, []);
  assert.equal(harness.store().stream.state, "blocked");
  assert.equal(harness.scheduler.pendingIds().length, 0);

  source.emit("ratings", [rating("late", 1)]);
  source.error();
  assert.deepEqual(harness.store().ratings, []);
  assert.equal(harness.scheduler.pendingIds().length, 0);
  assert.equal(harness.factory.sources.length, 1);
});

test("transport error clears daemon and schedules exactly one reconnect", () => {
  const harness = dataHarness();
  harness.controller.start();
  const first = harness.factory.sources[0];
  first.open();
  first.emit("daemon", daemonStatus());

  first.error();
  first.error();

  assert.equal(first.closeCount, 1);
  assert.equal(harness.store().daemon, null);
  assert.deepEqual(harness.store().stream, {
    state: "disconnected",
    last_event_at: null,
  });
  assert.equal(harness.scheduler.pendingIds().length, 1);

  harness.scheduler.runNext();
  assert.equal(harness.factory.sources.length, 2);
  assert.equal(harness.store().stream.state, "connecting");
});

test("epoch_blocked clears stale store and permanently suppresses reconnect for that controller", () => {
  const harness = dataHarness();
  harness.controller.start();
  const source = harness.factory.sources[0];
  source.open();
  source.emit("ratings", [rating("stale", 9999)]);
  source.emit("daemon", daemonStatus());

  source.emit("epoch_blocked", {
    evaluation_epoch: "national_tcp_policy_v1",
    epoch_initialized: false,
  });

  assert.equal(source.closed, true);
  assert.deepEqual(harness.store().ratings, []);
  assert.equal(harness.store().daemon, null);
  assert.equal(harness.store().stream.state, "blocked");
  assert.equal(harness.scheduler.pendingIds().length, 0);

  source.emit("ratings", [rating("late", 1)]);
  source.error();
  assert.deepEqual(harness.store().ratings, []);
  assert.equal(harness.scheduler.pendingIds().length, 0);
  assert.equal(harness.factory.sources.length, 1);
});

test("reconnect and cleanup fence late events from old and formerly-current sources", () => {
  const harness = dataHarness();
  const stop = harness.controller.start();
  const first = harness.factory.sources[0];
  first.error();
  harness.scheduler.runNext();
  const second = harness.factory.sources[1];

  first.emit("ratings", [rating("old", 1)]);
  assert.deepEqual(harness.store().ratings, []);
  second.emit("ratings", [rating("current", 2)]);
  assert.equal(harness.store().ratings[0].name, "current");

  stop();
  assert.equal(second.closed, true);
  const snapshot = structuredClone(harness.store());

  first.emit("ratings", [rating("late-old", 3)]);
  second.emit("ratings", [rating("late-current", 4)]);
  second.open();
  second.error();

  assert.deepEqual(harness.store(), snapshot);
  assert.equal(harness.factory.sources.length, 2);
  assert.equal(harness.scheduler.pendingIds().length, 0);
});

test("cleanup cancels a queued reconnect even if a stale timer callback fires later", () => {
  const harness = dataHarness();
  const stop = harness.controller.start();
  harness.factory.sources[0].error();
  const [cancelledTimer] = harness.scheduler.pendingIds();
  assert.ok(cancelledTimer);

  stop();
  harness.scheduler.fire(cancelledTimer, { force: true });

  assert.equal(harness.factory.sources.length, 1);
  assert.equal(harness.scheduler.pendingIds().length, 0);
});

test("evolution adapter dispatches live handlers and preserves block/error causality", () => {
  const factory = new FakeEventSourceFactory();
  const scheduler = new FakeScheduler();
  const calls = [];
  let handlers = {
    onConnect: () => calls.push(["connect"]),
    onStatus: (message, working) => calls.push(["status", message, working]),
    onPostPublicationHandoff: (data) => calls.push(["handoff", data.status, data.record_revision]),
    onEpochBlocked: (data) => calls.push(["blocked", data.epoch_state]),
    onDisconnect: (reason) => calls.push(["disconnect", reason]),
  };
  const controller = createEvolutionStreamController(
    () => handlers,
    "1".repeat(64),
    {
      createSource: factory.create,
      scheduler,
    },
  );
  const stop = controller.start();
  const first = factory.sources[0];
  assert.equal(first.url, `/api/evolution/stream?authority=${"1".repeat(64)}`);

  first.open();
  first.emit("status", { msg: "running", is_working: true });
  first.emit("post_publication_handoff", {
    schema_version: 1,
    authority: "post_publication_handoff_journal",
    status: "pending",
    state: "pending",
    blocked: false,
    version: 143,
    source_v: 142,
    workflow_run_id: "generation:143:workflow-v1",
    identity_digest: "a".repeat(64),
    publication_id: "b".repeat(64),
    record_revision: 2,
    next_tool: "run_archivist",
    issues: [],
    projection_digest: "c".repeat(64),
    stream_authority_digest: "1".repeat(64),
  });
  first.emitRaw("status", "{");
  first.emit("status", null);
  first.emit("status", { msg: "object-without-boolean" });
  assert.deepEqual(calls, [
    ["connect"],
    ["status", "running", true],
    ["handoff", "pending", 2],
  ]);

  handlers = {
    ...handlers,
    onStatus: (message, working) => calls.push(["new-status", message, working]),
  };
  first.emit("status", { msg: "new handler", is_working: false });
  first.error();
  first.error();
  assert.equal(scheduler.pendingIds().length, 1);
  scheduler.runNext();
  const second = factory.sources[1];

  first.emit("status", { msg: "stale", is_working: true });
  second.emit("epoch_blocked", {
    evaluation_epoch: "national_tcp_policy_v1",
    epoch_state: "reset_required",
    epoch_initialized: false,
    epoch_reset_receipt_digest: null,
    stream_authority_digest: null,
  });
  second.error();

  assert.deepEqual(calls, [
    ["connect"],
    ["status", "running", true],
    ["handoff", "pending", 2],
    ["new-status", "new handler", false],
    ["disconnect", "transport_error"],
    ["disconnect", "epoch_blocked"],
    ["blocked", "reset_required"],
  ]);
  assert.equal(scheduler.pendingIds().length, 0);
  stop();
});

test("same-authority handoff revision advances without fencing the evolution controller", () => {
  const factory = new FakeEventSourceFactory();
  const scheduler = new FakeScheduler();
  const revisions = [];
  const controller = createEvolutionStreamController(
    () => ({
      onPostPublicationHandoff: (data) => revisions.push(data.record_revision),
    }),
    "1".repeat(64),
    {
      createSource: factory.create,
      scheduler,
    },
  );
  const stop = controller.start();
  const source = factory.sources[0];
  const handoff = {
    schema_version: 1,
    authority: "post_publication_handoff_journal",
    status: "pending",
    state: "pending",
    blocked: false,
    version: 143,
    source_v: 142,
    workflow_run_id: "generation:143:workflow-v1",
    identity_digest: "a".repeat(64),
    publication_id: "b".repeat(64),
    record_revision: 2,
    next_tool: "run_archivist",
    issues: [],
    projection_digest: "c".repeat(64),
    stream_authority_digest: "1".repeat(64),
  };

  source.emit("post_publication_handoff", handoff);
  source.emit("post_publication_handoff", {
    ...handoff,
    status: "running",
    state: "running",
    record_revision: 3,
    projection_digest: "d".repeat(64),
  });

  assert.deepEqual(revisions, [2, 3]);
  assert.equal(source.closed, false);
  assert.equal(factory.sources.length, 1);
  assert.equal(scheduler.pendingIds().length, 0);
  stop();
});

test("all production stream events have rejecting minimal runtime schemas", () => {
  const validDataEvents = {
    ratings: [rating("national_v143", 1500)],
    daemon: daemonStatus(),
    rate_limit: { blocked: false },
    bots: { active: [botSummary()] },
    stats: {
      total_games: 1,
      total_strength_samples: 1,
      strength_sample_unit: "70_hand_match",
      hands_per_strength_sample: 70,
      total_pairs: 1,
      total_periods: 1,
      most_active_pair: "national_v143 vs national_v144",
      most_active_count: 1,
    },
    matches: [matchSummary()],
    generations: [{
      version: "v143",
      files: [
        "master_io.txt",
        `strict@${"1".repeat(32)}@critic_io.txt`,
      ],
    }],
    matrix: { bots: ["national_v143"], matrix: [[null]], source: "h2h", evidence_available: true },
    history: [{ period: 1, timestamp: "2026-07-15T00:00:00", ratings: {} }],
    h2h: { "national_v143 vs national_v144": { games: 1, a_wins: 1, b_wins: 0, draws: 0, win_rate: 1 } },
    bot_stats: { national_v143: { wins: 1, losses: 0, draws: 0, games: 1, win_rate: 1 } },
  };
  for (const [eventType, payload] of Object.entries(validDataEvents)) {
    assert.equal(validateDataStreamEvent(eventType, payload), true, eventType);
    assert.equal(validateDataStreamEvent(eventType, null), false, `${eventType}:null`);
  }
  assert.equal(validateDataStreamEvent("ratings", {}), false);
  assert.equal(validateDataStreamEvent("daemon", []), false);
  assert.equal(validateDataStreamEvent("generations", [{
    version: "v143",
    files: ["critic_io.txt.lock"],
  }]), false);
  assert.equal(validateDataStreamEvent("generations", [{
    version: "v143",
    files: [`strict@${"1".repeat(32)}@../critic_io.txt`],
  }]), false);

  const validEvolutionEvents = {
    history: { msg: "ok", status: "info", ts: 1 },
    status: { msg: "running", is_working: true, ts: 1 },
    io: { msg: "line", stream_type: "claude", role: "Master", ts: 1 },
    clear_io: { ts: 1 },
    eval_table: { rows: [rating("national_v143", 1500)], ts: 1 },
    daemon_stats: { total_matches: 1, total_periods: 1, total_games: 70, n_bots: 1, ts: 1 },
    header: { msg: "header", ts: 1 },
    cost: { role: "Master", cost_usd: 1, input_tokens: 2, output_tokens: 3, gen_total: 1, grand_total: 1, ts: 1 },
    generation_cost_policy: { generation_id: "generation:143:workflow-v1", spent_usd: 0, policy: generationCostPolicy(), ts: 1 },
    metrics: { current_v: 143, next_v: 144, ts: 1 },
    tool_call: { tool_name: "run_master", args: {}, role: "Orchestrator", ts: 1 },
    log_event: { level: "info", logger: "test", msg: "ok", ts: 1 },
    log_event_dropped: {
      level: "warn",
      logger: "test",
      msg: "throttled",
      dropped_count: 20,
      max_rate: 10,
      ts: 1,
    },
    system_event: { type: "pipeline.test", severity: "info", message: "ok", data: {}, ts: 1 },
    post_publication_handoff: {
      schema_version: 1,
      authority: "post_publication_handoff_journal",
      status: "running",
      state: "running",
      blocked: false,
      version: 143,
      source_v: 142,
      workflow_run_id: "generation:143:workflow-v1",
      identity_digest: "a".repeat(64),
      publication_id: "b".repeat(64),
      record_revision: 3,
      next_tool: "run_archivist",
      issues: [],
      projection_digest: "c".repeat(64),
      stream_authority_digest: "1".repeat(64),
    },
  };
  for (const [eventType, payload] of Object.entries(validEvolutionEvents)) {
    assert.equal(validateEvolutionStreamEvent(eventType, payload), true, eventType);
    assert.equal(validateEvolutionStreamEvent(eventType, null), false, `${eventType}:null`);
  }
  assert.equal(validateEvolutionStreamEvent("status", {}), false);
  assert.equal(validateEvolutionStreamEvent("eval_table", { rows: {} }), false);
  const handoff = validEvolutionEvents.post_publication_handoff;
  assert.equal(validateEvolutionStreamEvent("post_publication_handoff", {
    ...handoff,
    status: "none",
  }), false);
  assert.equal(validateEvolutionStreamEvent("post_publication_handoff", {
    ...handoff,
    status: "blocked",
    state: "blocked",
    blocked: true,
    next_tool: null,
    issues: [],
  }), false);
  assert.equal(validateEvolutionStreamEvent("post_publication_handoff", {
    ...handoff,
    status: "running",
    state: "pending",
  }), false);
});

test("actual Python producers satisfy the TypeScript stream validators", () => {
  const result = spawnSync(
    process.env.PYTHON || "python",
    ["tests/captureProducerEvents.py"],
    { cwd: process.cwd(), encoding: "utf8", timeout: 30_000 },
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
  const captured = JSON.parse(result.stdout);
  for (const [eventType, payload] of Object.entries(captured.evolution)) {
    assert.equal(
      validateEvolutionStreamEvent(eventType, payload),
      true,
      `evolution producer rejected: ${eventType}`,
    );
  }
  for (const [eventType, payload] of Object.entries(captured.data)) {
    assert.equal(
      validateDataStreamEvent(eventType, payload),
      true,
      `data producer rejected: ${eventType}`,
    );
  }
});

test("validated reset/authority identity changes re-arm with a new controller and close the old one", () => {
  const factory = new FakeEventSourceFactory();
  const scheduler = new FakeScheduler();
  let store = createInitialDataStore();
  const updateStore = (update) => {
    store = typeof update === "function" ? update(store) : update;
  };
  let activeKey = null;
  let stop = () => {};
  const replaceFor = (status) => {
    const nextKey = epochStreamAuthorityKey(status);
    if (nextKey === activeKey) return;
    stop();
    activeKey = nextKey;
    if (!activeKey) {
      store = createInitialDataStore();
      stop = () => {};
      return;
    }
    store = {
      ...createInitialDataStore(),
      stream: { state: "connecting", last_event_at: null },
    };
    const controller = createDataStreamController(updateStore, activeKey, {
      createSource: factory.create,
      scheduler,
    });
    stop = controller.start();
  };
  const authority = {
    evaluation_epoch: "national_tcp_policy_v1",
    epoch_state: "fresh_bootstrap_ready",
    epoch_initialized: true,
    version_authority_high_water: 142,
    reset_receipt_valid: true,
    reset_receipt_digest: "a".repeat(64),
    stream_authority_digest: "1".repeat(64),
    active_bots: [],
  };

  replaceFor(authority);
  const first = factory.sources[0];
  assert.equal(first.url, `/api/data/stream?authority=${"1".repeat(64)}`);
  first.emit("ratings", [rating("first", 1)]);
  assert.equal(store.ratings[0].name, "first");

  replaceFor({
    ...authority,
    reset_receipt_digest: "b".repeat(64),
    stream_authority_digest: "2".repeat(64),
  });
  const second = factory.sources[1];
  assert.equal(second.url, `/api/data/stream?authority=${"2".repeat(64)}`);
  assert.equal(first.closed, true);
  assert.deepEqual(store.ratings, []);
  first.emit("ratings", [rating("late-first", 2)]);
  second.emit("ratings", [rating("second", 3)]);
  assert.equal(store.ratings[0].name, "second");

  replaceFor({
    ...authority,
    reset_receipt_digest: "not-a-digest",
    stream_authority_digest: "3".repeat(64),
  });
  assert.equal(second.closed, true);
  assert.equal(factory.sources.length, 2);
  assert.deepEqual(store.ratings, []);
});

test("published high-water movement uses the backend replay identity and clears before reconnect", () => {
  const initial = {
    evaluation_epoch: "national_tcp_policy_v1",
    epoch_state: "fresh_bootstrap_ready",
    epoch_initialized: true,
    version_authority_high_water: 142,
    reset_receipt_valid: true,
    reset_receipt_digest: "a".repeat(64),
    stream_authority_digest: "1".repeat(64),
    active_bots: [],
  };
  const published = {
    ...initial,
    epoch_state: "strict_published",
    version_authority_high_water: 143,
    active_bots: ["national_v143"],
    stream_authority_digest: "2".repeat(64),
  };

  assert.equal(epochStreamAuthorityKey(initial), "1".repeat(64));
  assert.equal(epochStreamAuthorityKey(published), "2".repeat(64));
  assert.equal(epochStreamAuthorityKey({
    ...published,
    stream_authority_digest: "invalid",
  }), null);
});
