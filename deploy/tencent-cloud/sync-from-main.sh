#!/usr/bin/env bash
# ============================================================================
# sync-from-main.sh — pull new ideas/code from origin/main into this branch
# ============================================================================
# The cloud runtime tracks main for ideas and code, but NEVER pushes its
# evolution products (bots/national_cloud_v*, certificates) back to main.
# This helper merges origin/main into tencent-cloud-runtime inside the runtime
# checkout. It must be run when NO generation is active (the orchestrator
# should be stopped, or the generation parked).
#
# Conflicts: main carries national_v bots/docs that this branch may also edit
# in the cloud namespace. Resolve conflicts keeping the CLOUD versions for
# bots/national_cloud_v* and official_certificates/national_cloud_v*, and take
# main's version for everything under web/, sever/, scripts/, docs/ (ideas).
# ============================================================================
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "ERROR: not inside a git checkout" >&2; exit 1; }
cd "$ROOT"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" != "tencent-cloud-runtime" ]]; then
  echo "ERROR: on '${BRANCH}', run this in the runtime checkout on tencent-cloud-runtime" >&2
  exit 1
fi

echo "== syncing origin/main -> ${BRANCH} =="
echo "(ensure no generation is running before continuing)"
echo

git fetch --tags origin

# Show what's incoming before touching the worktree.
AHEAD_BEHIND="$(git rev-list --left-right --count HEAD...origin/main)"
AHEAD="$(echo "$AHEAD_BEHIND" | awk '{print $1}')"
BEHIND="$(echo "$AHEAD_BEHIND" | awk '{print $2}')"
echo "ahead of origin/main : ${AHEAD} commits (your cloud products + ideas)"
echo "behind origin/main   : ${BEHIND} commits (new ideas to pull)"

if [[ "$BEHIND" -eq 0 ]]; then
  echo "Already up to date with origin/main. Nothing to merge."
  exit 0
fi

echo
echo "Incoming commits (origin/main):"
git log --oneline HEAD..origin/main | head -20

echo
read -r -p "Merge origin/main into ${BRANCH}? [y/N] " ans
if [[ "${ans,,}" != "y" ]]; then
  echo "aborted"; exit 0
fi

git merge origin/main -m "Merge origin/main ideas into tencent-cloud-runtime"

echo
echo "== merge complete =="
echo "If conflicts arose on cloud-namespace files, resolve keeping the cloud side."
echo "Push the merged branch so the runtime clone and GitHub agree:"
echo "  git push origin tencent-cloud-runtime"
echo
echo "Products (national_cloud_v*) stay on this branch; origin/main is untouched."
