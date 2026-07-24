#!/usr/bin/env bash
# ============================================================================
# seed-cloud-namespace.sh — one-time epoch bootstrap for the cloud namespace
# ============================================================================
# The cloud runtime uses an ISOLATED tag namespace (national-cloud-bot-v* /
# national-cloud-high-water-v*). A fresh checkout inherits main's national_v*
# tags but has NO cloud-namespace tags, so resolve_version_namespace_authority
# finds no paired version and policy_epoch_initialization parks in
# version_authority_requires_recovery (cannot start a generation).
#
# This script seeds the cloud namespace by pointing a paired v143 completion +
# high-water tag at the SAME commit that main's national-bot-v143 points to,
# and mirrors bots/national_v143 -> bots/national_cloud_v143 so the strict bot
# directory exists in the cloud namespace. After this, epoch initializes via
# the strict_published path and the runtime can start producing v144+.
#
# ONE-TIME, IDEMPOTENT: safe to re-run; it refuses to overwrite a cloud tag
# that already exists. Run inside .evolution_pok on tencent-cloud-runtime.
# ============================================================================
set -euo pipefail

ERR=0
ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || ERR=1
if [[ "$ERR" -ne 0 || -z "${ROOT}" ]]; then
  echo "ERROR: run this inside the .evolution_pok git checkout" >&2
  exit 1
fi
cd "$ROOT"

# Require the cloud namespace env so the checks below are unambiguous.
: "${POK_BOT_PREFIX:=national_cloud_v}"
: "${POK_TAG_PREFIX:=national-cloud-bot-v}"
: "${POK_HIGH_WATER_TAG_PREFIX:=national-cloud-high-water-v}"
export POK_BOT_PREFIX POK_TAG_PREFIX POK_HIGH_WATER_TAG_PREFIX

BASELINE_VERSION=143
COMPLETION_TAG="${POK_TAG_PREFIX}${BASELINE_VERSION}"
HIGH_WATER_TAG="${POK_HIGH_WATER_TAG_PREFIX}${BASELINE_VERSION}"
SOURCE_BOT_DIR="bots/national_v${BASELINE_VERSION}"
CLOUD_BOT_DIR="bots/${POK_BOT_PREFIX}${BASELINE_VERSION}"

echo "== cloud-namespace seed (v${BASELINE_VERSION}) =="

# 1. Resolve the commit that main's canonical completion tag points at. This is
#    the provenance anchor: the cloud v143 is the same published artifact, just
#    re-labelled in the cloud namespace.
SOURCE_TAG="national-bot-v${BASELINE_VERSION}"
if ! BASELINE_COMMIT="$(git rev-parse --verify -q "refs/tags/${SOURCE_TAG}^{commit}")" \
  || [[ -z "$BASELINE_COMMIT" ]]; then
  echo "ERROR: ${SOURCE_TAG} tag not found. Fetch tags first:" >&2
  echo "  git fetch --tags origin" >&2
  exit 1
fi
echo "baseline commit (${SOURCE_TAG}): ${BASELINE_COMMIT}"

# 2. Refuse to clobber an existing cloud tag (idempotent guard).
if git rev-parse --verify -q "refs/tags/${COMPLETION_TAG}" >/dev/null \
  || git rev-parse --verify -q "refs/tags/${HIGH_WATER_TAG}" >/dev/null; then
  echo "NOTE: cloud namespace already seeded (${COMPLETION_TAG} or ${HIGH_WATER_TAG} exists)."
  echo "      To re-seed you must first delete both cloud tags manually."
  exit 0
fi

# 3. Mirror the v143 bot directory into the cloud namespace (the artifact bytes
#    are identical; only the directory label changes). Skip if it already exists.
if [[ ! -d "$CLOUD_BOT_DIR" ]]; then
  if [[ ! -d "$SOURCE_BOT_DIR" ]]; then
    echo "ERROR: ${SOURCE_BOT_DIR} not found in this checkout." >&2
    exit 1
  fi
  cp -a "$SOURCE_BOT_DIR" "$CLOUD_BOT_DIR"
  echo "mirrored ${SOURCE_BOT_DIR} -> ${CLOUD_BOT_DIR}"
else
  echo "${CLOUD_BOT_DIR} already present"
fi

# 4. Create the paired annotated tags pointing at the SAME commit. The pairing
#    (completion + high-water peel to one commit) is exactly what
#    resolve_version_namespace_authority requires.
git tag -a "$COMPLETION_TAG" "$BASELINE_COMMIT" -m \
  "cloud-namespace seed completion tag for v${BASELINE_VERSION} (mirrors ${SOURCE_TAG})"
git tag -a "$HIGH_WATER_TAG" "$BASELINE_COMMIT" -m \
  "cloud-namespace seed high-water tag for v${BASELINE_VERSION} (mirrors national-high-water-v${BASELINE_VERSION})"
echo "created paired tags: ${COMPLETION_TAG}, ${HIGH_WATER_TAG} -> ${BASELINE_COMMIT}"

echo
echo "== seed complete =="
echo "Epoch should now initialize via strict_published on the cloud namespace."
echo "Verify with:"
echo "  cd web && python3 -c 'import sys; sys.path.insert(0,\"core\"); \\"
echo "    from epoch_authority import policy_epoch_initialization; \\"
echo "    import json; print(json.dumps(policy_epoch_initialization(), indent=2))'"
echo
echo "Push the seed tags so origin/tencent-cloud-runtime carries the namespace:"
echo "  git push origin ${COMPLETION_TAG} ${HIGH_WATER_TAG}"
