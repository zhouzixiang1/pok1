#!/usr/bin/env bash
# ============================================================================
# seed-cloud-namespace.sh — DEPRECATED (version-1 floor redesign)
# ============================================================================
# This script was written for an earlier design that seeded the cloud
# namespace by mirroring main's national-bot-v143 into
# national-cloud-bot-v143 so epoch could initialize via strict_published.
#
# THAT DESIGN IS SUPERSEDED. The cloud branch now restarts version numbering
# from 1 (ARCHIVED_VERSION_HIGH_WATER=0, FIRST_STRICT_POLICY_VERSION=1 in
# web/core/bot_namespace.py). A fresh cloud checkout has NO paired cloud tags,
# so resolve_version_namespace_authority falls back to the archived high-water
# (0) and policy_epoch_initialization initializes via the fresh_bootstrap_ready
# path — NO seed tag and NO mirrored v143 directory are required or wanted.
# Seeding v143 here would create a version-143 floor that contradicts the
# code's version-1 floor and would block the first-strict bootstrap.
#
# This script is retained for historical reference only. Do NOT run it on the
# current cloud branch. If you need to re-seed a legacy deployment that still
# uses the v143 floor, restore an older checkout of this file.
# ============================================================================
set -euo pipefail

echo "seed-cloud-namespace.sh is DEPRECATED for the version-1 cloud floor." >&2
echo "The cloud epoch initializes via fresh_bootstrap_ready (high-water 0," >&2
echo "first target national_cloud_v1) WITHOUT any seed tag. See AGENTS.md." >&2
echo "Refusing to run. Exiting 0 so setup.sh's idempotent call is a no-op." >&2
exit 0

