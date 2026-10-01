# Shared helpers for the backup scripts. Sourced, not executed.
set -euo pipefail
umask 027  # backups contain personal and financial data
CONF=${BACKUP_CONF:-/etc/backend/backup.conf}
[[ -f "$CONF" ]] || { echo "Missing $CONF (copy deploy/backup.conf.template)" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONF"
: "${BACKUP_DIR:?}" "${DB_NAME:?}" "${STATUS_FILE:?}"
KEEP_DAILY_DAYS=${KEEP_DAILY_DAYS:-14}
KEEP_BASE_BACKUPS=${KEEP_BASE_BACKUPS:-4}

as_postgres() { runuser -u postgres -- "$@"; }
# Server-side tools (pg_archivecleanup, pg_ctl) live in the versioned bin dir on Debian/Ubuntu.
PG_BIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1)
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Merge key=value pairs into the JSON status file (atomic replace, world-readable).
update_status() {
    python3 - "$STATUS_FILE" "$@" <<'PY'
import json, os, sys, tempfile
path, pairs = sys.argv[1], sys.argv[2:]
try:
    data = json.load(open(path))
except Exception:
    data = {}
for pair in pairs:
    key, _, value = pair.partition("=")
    data[key] = value
os.makedirs(os.path.dirname(path), exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as f:
    json.dump(data, f, indent=2, sort_keys=True)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

copy_offsite() {
    if [[ -n "${OFFSITE_DIR:-}" ]]; then
        mkdir -p "$OFFSITE_DIR"
        rsync -a --delete "$BACKUP_DIR"/ "$OFFSITE_DIR"/
        update_status "offsite_copy_at=$(now_iso)" "offsite_target=$OFFSITE_DIR"
    elif [[ -n "${OFFSITE_RSYNC:-}" ]]; then
        rsync -a --delete -e "ssh -o BatchMode=yes" "$BACKUP_DIR"/ "$OFFSITE_RSYNC"/
        update_status "offsite_copy_at=$(now_iso)" "offsite_target=$OFFSITE_RSYNC"
    else
        echo "WARNING: no OFFSITE_DIR/OFFSITE_RSYNC set: backups exist on this disk only." >&2
        update_status "offsite_target="
    fi
}
