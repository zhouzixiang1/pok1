#!/usr/bin/env node
/**
 * Bind a generated production SPA bundle to the frontend sources that Vite
 * actually consumes.  `pokctl start --no-build` verifies this receipt before
 * it can stop or replace an owned server, so the dashboard cannot silently
 * serve an older status-authority implementation after source changes.
 */
import { createHash } from "node:crypto";
import { lstat, mkdir, readFile, readdir, rename, writeFile } from "node:fs/promises";
import { dirname, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const RECEIPT_SCHEMA_VERSION = 1;
const RECEIPT_KIND = "national_tcp_policy_v1_frontend_static_build";
const scriptDir = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(scriptDir, "../../..");
const frontendRoot = resolve(repositoryRoot, "web/frontend");
const staticRoot = resolve(repositoryRoot, "web/server/static");

// These are the source-controlled inputs to the Vite production build.  Test
// files and documentation intentionally do not invalidate an already built
// production bundle, while any UI, public asset, toolchain lock, or build
// configuration change does.
const BUILD_INPUTS = [
  "index.html",
  "package.json",
  "package-lock.json",
  "postcss.config.js",
  "tsconfig.json",
  "tsconfig.app.json",
  "tsconfig.node.json",
  "vite.config.ts",
  "banner.png",
  "src",
  "public",
  "scripts/static-build-receipt.mjs",
];

function pathInside(root, candidate) {
  const rel = relative(root, candidate);
  return rel === "" || (!rel.startsWith(`..${sep}`) && rel !== "..");
}

async function regularFile(path) {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink()) {
    throw new Error(`expected regular non-symlink file: ${path}`);
  }
}

async function collectInputFiles(path, relativePath, records) {
  const info = await lstat(path);
  if (info.isSymbolicLink()) {
    throw new Error(`source-controlled build input must not be a symlink: ${relativePath}`);
  }
  if (info.isFile()) {
    records.push({ absolutePath: path, relativePath });
    return;
  }
  if (!info.isDirectory()) {
    throw new Error(`unsupported build input type: ${relativePath}`);
  }
  const entries = (await readdir(path, {
    withFileTypes: true,
  })).sort((left, right) => left.name.localeCompare(right.name));
  for (const entry of entries) {
    const childPath = resolve(path, entry.name);
    const childRelative = `${relativePath}/${entry.name}`;
    if (entry.isSymbolicLink()) {
      throw new Error(`source-controlled build input must not be a symlink: ${childRelative}`);
    }
    if (entry.isDirectory() || entry.isFile()) {
      await collectInputFiles(childPath, childRelative, records);
      continue;
    }
    throw new Error(`unsupported build input type: ${childRelative}`);
  }
}

async function buildInputFingerprint() {
  const records = [];
  for (const input of BUILD_INPUTS) {
    await collectInputFiles(resolve(frontendRoot, input), input, records);
  }
  records.sort((left, right) => left.relativePath.localeCompare(right.relativePath));

  const fingerprint = createHash("sha256");
  for (const record of records) {
    const bytes = await readFile(record.absolutePath);
    const contentHash = createHash("sha256").update(bytes).digest("hex");
    fingerprint.update(record.relativePath, "utf8");
    fingerprint.update("\0", "utf8");
    fingerprint.update(contentHash, "ascii");
    fingerprint.update("\n", "utf8");
  }
  return {
    source_file_count: records.length,
    source_fingerprint: fingerprint.digest("hex"),
  };
}

function receiptFromFingerprint(fingerprint) {
  return {
    receipt_kind: RECEIPT_KIND,
    schema_version: RECEIPT_SCHEMA_VERSION,
    source_file_count: fingerprint.source_file_count,
    source_fingerprint: fingerprint.source_fingerprint,
  };
}

function validateReceipt(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("receipt must be an object");
  }
  const keys = Object.keys(value).sort();
  const expectedKeys = [
    "receipt_kind",
    "schema_version",
    "source_file_count",
    "source_fingerprint",
  ];
  if (keys.length !== expectedKeys.length || keys.some((key, index) => key !== expectedKeys[index])) {
    throw new Error("receipt keys do not match the source-controlled schema");
  }
  if (value.receipt_kind !== RECEIPT_KIND || value.schema_version !== RECEIPT_SCHEMA_VERSION) {
    throw new Error("receipt kind or schema version is not supported");
  }
  if (!Number.isInteger(value.source_file_count) || value.source_file_count <= 0) {
    throw new Error("receipt source_file_count is invalid");
  }
  if (typeof value.source_fingerprint !== "string" || !/^[0-9a-f]{64}$/.test(value.source_fingerprint)) {
    throw new Error("receipt source_fingerprint is invalid");
  }
  return value;
}

async function assertReceiptPath(path) {
  const absolute = resolve(path);
  if (!pathInside(staticRoot, absolute) || absolute === staticRoot) {
    throw new Error("receipt path must stay under web/server/static");
  }
  await regularFile(absolute);
  return absolute;
}

async function writeReceipt(path, expectedSourceFingerprint = null) {
  const absolute = resolve(path);
  const distRoot = resolve(frontendRoot, "dist");
  if (!pathInside(distRoot, absolute) || absolute === distRoot) {
    throw new Error("build receipt output must stay under web/frontend/dist");
  }
  const fingerprint = await buildInputFingerprint();
  if (
    expectedSourceFingerprint !== null
    && fingerprint.source_fingerprint !== expectedSourceFingerprint
  ) {
    throw new Error("frontend build inputs changed while the production build was running");
  }
  const receipt = receiptFromFingerprint(fingerprint);
  await mkdir(dirname(absolute), { recursive: true });
  const temporary = `${absolute}.tmp-${process.pid}-${Date.now()}`;
  await writeFile(temporary, `${JSON.stringify(receipt, null, 2)}\n`, {
    encoding: "utf8",
    flag: "wx",
  });
  await rename(temporary, absolute);
  process.stdout.write(`static build receipt written: ${receipt.source_fingerprint}\n`);
}

async function verifyReceipt(path) {
  const absolute = await assertReceiptPath(path);
  const parsed = validateReceipt(JSON.parse(await readFile(absolute, "utf8")));
  const expected = receiptFromFingerprint(await buildInputFingerprint());
  if (
    parsed.source_file_count !== expected.source_file_count
    || parsed.source_fingerprint !== expected.source_fingerprint
  ) {
    throw new Error("static bundle receipt does not match current frontend build inputs");
  }
  process.stdout.write(`static build receipt verified: ${parsed.source_fingerprint}\n`);
}

async function main() {
  const [command, path, ...extra] = process.argv.slice(2);
  if (command === "--source-fingerprint" && path === undefined && extra.length === 0) {
    const fingerprint = await buildInputFingerprint();
    process.stdout.write(`${fingerprint.source_fingerprint}\n`);
    return;
  }
  if (command === "--write") {
    if (
      typeof path !== "string"
      || !(
        extra.length === 0
        || (
          extra.length === 2
          && extra[0] === "--expect-source-fingerprint"
          && /^[0-9a-f]{64}$/.test(extra[1])
        )
      )
    ) {
      throw new Error("usage: static-build-receipt.mjs --write <dist-receipt> [--expect-source-fingerprint <sha256>]");
    }
    await writeReceipt(path, extra.length === 2 ? extra[1] : null);
    return;
  }
  if (command === "--verify") {
    if (typeof path !== "string" || extra.length !== 0) {
      throw new Error("usage: static-build-receipt.mjs --verify <static-receipt>");
    }
    await verifyReceipt(path);
    return;
  }
  throw new Error("usage: static-build-receipt.mjs --source-fingerprint | --write <dist-receipt> [--expect-source-fingerprint <sha256>] | --verify <static-receipt>");
}

main().catch((error) => {
  process.stderr.write(`static build receipt verification failed: ${error.message}\n`);
  process.exitCode = 1;
});
