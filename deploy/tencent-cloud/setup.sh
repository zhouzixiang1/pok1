#!/usr/bin/env bash
# ============================================================================
# setup.sh — one-time Tencent Cloud runtime deployment
# ============================================================================
# Builds the dual-checkout structure on this server and installs the systemd
# service. Run once from the operator checkout (this file's parent's parent).
#
# Layout produced:
#   /home/ubuntu/pok1                     operator checkout (this one)
#   /home/ubuntu/pok1/.evolution_pok      autonomous runtime clone (independent)
#
# The runtime clone publishes into tencent-cloud-runtime with the cloud tag
# namespace; origin/main is never disturbed.
# ============================================================================
set -euo pipefail

OPERATOR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_ROOT="${OPERATOR_ROOT}/.evolution_pok"
SERVICE_DST="/etc/systemd/system/pok-evolution.service"
ENV_FILE="${OPERATOR_ROOT}/deploy/tencent-cloud/env.runtime"

echo "== Tencent Cloud runtime setup =="
echo "operator checkout : ${OPERATOR_ROOT}"
echo "runtime clone     : ${RUNTIME_ROOT}"

# 1. Verify operator checkout is on tencent-cloud-runtime.
cd "$OPERATOR_ROOT"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" != "tencent-cloud-runtime" ]]; then
  echo "ERROR: operator checkout is on '${BRANCH}', expected 'tencent-cloud-runtime'." >&2
  echo "       Switch with: git checkout tencent-cloud-runtime" >&2
  exit 1
fi
REMOTE="$(git remote get-url origin)"
echo "remote origin     : ${REMOTE}"

# 2. Create the autonomous runtime clone if absent. It is an INDEPENDENT clone
#    (not a worktree) so reset_national_tcp_policy_epoch's identity checks and
#    the .evolution_pok directory-name contract are satisfied. It checks out the
#    same tencent-cloud-runtime branch.
if [[ ! -d "$RUNTIME_ROOT/.git" ]]; then
  echo
  echo "== cloning runtime checkout =="
  git clone --branch tencent-cloud-runtime "$OPERATOR_ROOT" "$RUNTIME_ROOT"
  # Point the clone's origin at the real GitHub remote, not the local operator
  # checkout, so publications reach GitHub.
  git -C "$RUNTIME_ROOT" remote set-url origin "$REMOTE"
  echo "runtime clone origin -> ${REMOTE}"
else
  echo "runtime clone already exists at ${RUNTIME_ROOT}"
fi

# 3. Verify env.runtime has POK_PYTHON set to a real interpreter.
echo
echo "== verifying environment =="
# shellcheck disable=SC1090
source "$ENV_FILE"
if [[ ! -x "${POK_PYTHON:-}" ]]; then
  echo "WARNING: POK_PYTHON='${POK_PYTHON:-<unset>}' is not executable." >&2
  echo "         Edit ${ENV_FILE} and set POK_PYTHON to the interpreter that has" >&2
  echo "         web/sever requirements installed, then re-run this script." >&2
fi
echo "POK_PYTHON        : ${POK_PYTHON:-<unset>}"

# 4. Seed the cloud namespace (idempotent). Must run inside the runtime clone
#    so tags/bot directory land there.
echo
echo "== seeding cloud namespace (idempotent) =="
cd "$RUNTIME_ROOT"
git fetch --tags origin >/dev/null 2>&1 || true
POK_BOT_PREFIX=national_cloud_v \
POK_TAG_PREFIX=national-cloud-bot-v \
POK_HIGH_WATER_TAG_PREFIX=national-cloud-high-water-v \
  bash "${OPERATOR_ROOT}/deploy/tencent-cloud/seed-cloud-namespace.sh" || true

# 5. Install the systemd unit (needs sudo).
echo
echo "== installing systemd service =="
if sudo test -f "$SERVICE_DST"; then
  echo "service already installed at ${SERVICE_DST}"
else
  sudo cp "${OPERATOR_ROOT}/deploy/tencent-cloud/pok-evolution.service" "$SERVICE_DST"
  echo "copied unit -> ${SERVICE_DST}"
fi
sudo systemctl daemon-reload
sudo systemctl enable pok-evolution >/dev/null
echo "service enabled. Start with: sudo systemctl start pok-evolution"

echo
echo "== setup complete =="
echo "Next:"
echo "  1. Edit ${ENV_FILE}: set ANTHROPIC_API_KEY / POK_LLM_MODEL for generations."
echo "  2. Build the frontend once: cd ${RUNTIME_ROOT}/web/frontend && npm i && npm run build"
echo "     (then the --no-build service flag reuses the static receipt)."
echo "  3. sudo systemctl start pok-evolution"
echo "  4. journalctl -u pok-evolution -f"
