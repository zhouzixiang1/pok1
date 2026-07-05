#!/usr/bin/env bash
# Render and install the pok web stdout logrotate config for this checkout.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT/scripts/pok.logrotate"
DEST="/etc/logrotate.d/pok"
DRY_RUN=0
POK_USER="${POK_LOGROTATE_USER:-$(id -un)}"
POK_GROUP="${POK_LOGROTATE_GROUP:-$(id -gn)}"

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --dry-run         print rendered config instead of installing it
  --dest PATH       destination path (default: /etc/logrotate.d/pok)
  --user USER       user for logrotate su directive (default: current user)
  --group GROUP     group for logrotate su directive (default: current group)
  -h, --help        show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --dest) DEST="$2"; shift 2 ;;
        --user) POK_USER="$2"; shift 2 ;;
        --group) POK_GROUP="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

if [ ! -f "$TEMPLATE" ]; then
    echo "Missing template: $TEMPLATE" >&2
    exit 1
fi

config="$(<"$TEMPLATE")"
config="${config//__POK_ROOT__/$ROOT}"
config="${config//__POK_USER__/$POK_USER}"
config="${config//__POK_GROUP__/$POK_GROUP}"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
printf '%s\n' "$config" > "$tmp"

if grep -q '__POK_' "$tmp"; then
    echo "Template substitution failed; unresolved placeholder remains." >&2
    exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
    cat "$tmp"
    exit 0
fi

dest_dir="$(dirname "$DEST")"
if [ ! -d "$dest_dir" ]; then
    echo "Destination directory does not exist: $dest_dir" >&2
    exit 1
fi

if [ -w "$dest_dir" ] && { [ ! -e "$DEST" ] || [ -w "$DEST" ]; }; then
    install -m 0644 "$tmp" "$DEST"
else
    sudo install -m 0644 "$tmp" "$DEST"
fi

echo "Installed logrotate config: $DEST"
echo "Rotating: $ROOT/web/logs/server.stdout.log"
