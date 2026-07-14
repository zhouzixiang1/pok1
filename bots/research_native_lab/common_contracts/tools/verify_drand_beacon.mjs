#!/usr/bin/env node
/*
 * Offline adapter for the hash-pinned drand-client ESM bundle.
 *
 * Python validates and canonicalises every saved relay payload first.  This
 * adapter deliberately has no HTTP client: it supplies those exact bytes to
 * drand-client's public fetchBeacon API, which performs the official BLS
 * verification path.  A successful exit is therefore not a caller assertion.
 */

import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

if (process.argv.length !== 4) {
  fail("usage: verify_drand_beacon.mjs OFFICIAL_MODULE REQUEST_JSON");
}

const modulePath = process.argv[2];
const requestPath = process.argv[3];

let request;
try {
  request = JSON.parse(await readFile(requestPath, { encoding: "utf8" }));
} catch (error) {
  fail(`invalid verification request: ${error.message}`);
}

if (
  request === null ||
  typeof request !== "object" ||
  Array.isArray(request) ||
  request.schema !== "drand-offline-verification-v1" ||
  request.chain_info === null ||
  typeof request.chain_info !== "object" ||
  !Array.isArray(request.beacons) ||
  request.beacons.length < 1
) {
  fail("invalid verification request schema");
}

let drand;
try {
  drand = await import(pathToFileURL(modulePath).href);
} catch (error) {
  fail(`cannot import official drand verifier: ${error.message}`);
}
if (typeof drand.fetchBeacon !== "function") {
  fail("official drand verifier does not export fetchBeacon");
}

const byRound = new Map();
for (const beacon of request.beacons) {
  if (
    beacon === null ||
    typeof beacon !== "object" ||
    !Number.isSafeInteger(beacon.round) ||
    beacon.round < 1 ||
    byRound.has(beacon.round)
  ) {
    fail("invalid or duplicate beacon round");
  }
  byRound.set(beacon.round, Object.freeze({ ...beacon }));
}

const chainInfo = Object.freeze({ ...request.chain_info });
const chain = Object.freeze({
  baseUrl: "offline://saved-cross-fetch-evidence",
  info: async () => chainInfo,
});
const client = Object.freeze({
  options: Object.freeze({
    disableBeaconVerification: false,
    noCache: true,
    chainVerificationParams: Object.freeze({
      chainHash: chainInfo.hash,
      publicKey: chainInfo.public_key,
    }),
  }),
  chain: () => chain,
  get: async (round) => {
    if (!byRound.has(round)) {
      throw new Error(`round ${round} absent from saved evidence`);
    }
    return byRound.get(round);
  },
  latest: async () => {
    throw new Error("latest is forbidden for a frozen round");
  },
});

try {
  const verifiedRounds = [];
  for (const beacon of request.beacons) {
    const verified = await drand.fetchBeacon(client, beacon.round);
    if (verified.round !== beacon.round || verified.signature !== beacon.signature) {
      fail("official verifier returned different beacon material");
    }
    verifiedRounds.push(beacon.round);
  }
  process.stdout.write(
    `${JSON.stringify({
      schema: "drand-offline-verification-result-v1",
      verified: true,
      verified_rounds: verifiedRounds,
    })}\n`,
  );
} catch (error) {
  fail(`official drand BLS verification failed: ${error.message}`);
}
