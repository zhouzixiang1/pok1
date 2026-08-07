import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

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
  const src = readFileSync(join(root, "lib/notStuckReasons.ts"), "utf8");
  assert.match(src, /consumer_parked/);
  assert.match(src, /eval_wait/);
  // Removed codes must NOT be present (no backend source-of-truth).
  assert.doesNotMatch(src, /post_publication_handoff_running/);
  assert.doesNotMatch(src, /eval_wait_degraded/);
  assert.doesNotMatch(src, /staging_async_cert/);
});
