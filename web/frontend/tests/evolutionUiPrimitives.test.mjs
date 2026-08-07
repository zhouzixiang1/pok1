import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { notStuckLabel, NOT_STUCK_REASON_CODES } from "../node_modules/.tmp/sse-tests/lib/notStuckReasons.js";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "src");

test("evolution ui primitives export surface language", () => {
  const index = readFileSync(join(root, "components/evolution/ui/index.ts"), "utf8");
  for (const name of [
    "EvolutionSurface",
    "EvolutionSection",
    "EvolutionStatusBadge",
    "EvolutionStepperTrack",
    "EvolutionStreamShell",
    "STATUS_TONE_CLASSES",
  ]) {
    assert.match(index, new RegExp(name));
  }
  const tokens = readFileSync(join(root, "components/evolution/ui/tokens.ts"), "utf8");
  assert.match(tokens, /park/);
  assert.match(tokens, /rounded-2xl/);
});

test("App redirects legacy routes to the 5 core pages", () => {
  const app = readFileSync(join(root, "App.tsx"), "utf8");
  // IA-merge (2026-07-30) collapsed the 15-page layout into 5 core pages
  // (/, /generation, /bots, /llm, /control). Legacy routes redirect there.
  // /evolution and /agents both now redirect to /generation (the sole full
  // generation stepper + research SSE page); /bots-inventory -> /bots.
  assert.match(app, /path="\/evolution".*Navigate to="\/generation"/s);
  assert.match(app, /path="\/agents".*Navigate to="\/generation"/s);
  assert.match(app, /path="\/bots-inventory".*Navigate to="\/bots"/s);
});

test("HandoffEightStep lists Chinese step names", () => {
  const src = readFileSync(join(root, "components/evolution/HandoffEightStep.tsx"), "utf8");
  for (const label of [
    "稳定性观察",
    "回收信号",
    "优先评测",
    "归档轮转",
    "日志清理",
    "池回收",
    "周期标注",
    "管家收尾",
  ]) {
    assert.match(src, new RegExp(label));
  }
});

test("notStuckReasons covers park and eval_wait", () => {
  // The two live codes must resolve.
  assert.ok(notStuckLabel("consumer_parked"));
  assert.ok(notStuckLabel("eval_wait"));
  assert.ok(NOT_STUCK_REASON_CODES.consumer_parked);
  assert.ok(NOT_STUCK_REASON_CODES.eval_wait);
  // Removed codes must NOT resolve (no backend source-of-truth). Asserting
  // against the exported map, not the raw source text, so an explanatory
  // comment naming a removed code does not false-trigger.
  for (const removed of [
    "post_publication_handoff_running",
    "eval_wait_degraded",
    "staging_async_cert",
    "quota_wait",
    "draft_preparing",
    "official_certifying",
  ]) {
    assert.equal(notStuckLabel(removed), null, `removed code ${removed} must not resolve`);
    assert.ok(!(removed in NOT_STUCK_REASON_CODES), `removed key ${removed} must not be a map entry`);
  }
});
