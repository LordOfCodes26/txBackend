# Shared helpers for the backup scripts. Sourced, not executed.
set -euo pipefail
umask 027  # backups contain personal and financial data
CONF=${BACKUP_CONF:-/etc/backend/backup.conf}
[[ -f "$CONF" ]] || { echo "Missing $CONF (copy deploy/backup.conf.template)" >&2; exit 1; }
# shellcheck disable=SC1090
. "$CONF"
: "${BACKUP_DIR:?}" "${DB_NAME:?}" "${STATUS_FILE:?}"
KEEP_DAILY_DAYS=${KEEP_DAILY_DAYS:-14}
KEEP_BASE_BACKUPS=${KEEP_BASE_BACKUPS:-7}
DISK_PRUNE_PERCENT=${DISK_PRUNE_PERCENT:-85}

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

# Delete archived WAL older than the oldest kept base backup: no restore can start before
# it. -x .gz: the archive is gzipped (plain files from before that are matched too).
prune_wal() {
    local oldest first_wal
    oldest=$(ls -1d "$BACKUP_DIR"/base/*/ 2>/dev/null | sort | head -1)
    [[ -n "$oldest" ]] || return 0
    first_wal=$(tar -xzOf "$oldest/base.tar.gz" backup_label 2>/dev/null \
                | sed -n 's/^START WAL LOCATION: .* (file \(.*\))$/\1/p')
    [[ -n "$first_wal" ]] || return 0
    as_postgres "$PG_BIN/pg_archivecleanup" -x .gz "$BACKUP_DIR/wal" "$first_wal"
    # pg_archivecleanup leaves the backup history files (one small file per base backup).
    find "$BACKUP_DIR/wal" -maxdepth 1 -name '*.backup*' ! -newer "$oldest" -delete
}

disk_percent() { df --output=pcent "$1" | tail -1 | tr -dc 0-9; }

# The backup disk is nearly full: give up the oldest point-in-time history, then the oldest
# nightly dumps, rather than let the database stop when the disk is full. Always kept: the
# newest base backup with the WAL after it, and the newest 3 dumps.
free_space_guard() {
    local pruned=0 bases dumps
    while (( $(disk_percent "$BACKUP_DIR") >= DISK_PRUNE_PERCENT )); do
        mapfile -t bases < <(ls -1d "$BACKUP_DIR"/base/*/ 2>/dev/null | sort)
        if (( ${#bases[@]} > 1 )); then
            echo "Disk $(disk_percent "$BACKUP_DIR")% full: deleting base backup ${bases[0]}" >&2
            rm -rf "${bases[0]}"
            prune_wal
        else
            mapfile -t dumps < <(ls -1 "$BACKUP_DIR"/db/*.dump 2>/dev/null | sort)
            (( ${#dumps[@]} > 3 )) || break
            echo "Disk $(disk_percent "$BACKUP_DIR")% full: deleting dump ${dumps[0]}" >&2
            rm -f "${dumps[0]}" "${dumps[0]}.sha256"
        fi
        pruned=1
    done
    if (( pruned )); then
        update_status "space_pruned_at=$(now_iso)"
    fi
    if (( $(disk_percent "$BACKUP_DIR") >= DISK_PRUNE_PERCENT )); then
        echo "WARNING: the backup disk is still $(disk_percent "$BACKUP_DIR")% full: free space or use a bigger disk." >&2
    fi
}

# The filesystem a path is on: its own, or its nearest existing parent's.
fs_of() {
    local path=$1
    while [[ ! -e "$path" ]]; do path=$(dirname "$path"); done
    stat -c %d "$path"
}

# The off-site copy didn't happen: say so in the status file (GET /health/backup/ reports
# it) and fail the run. $1: NOT_MOUNTED or COPY_FAILED; $2: the reason, for the log.
offsite_failed() {
    echo "OFF-SITE COPY FAILED: $2" >&2
    update_status "offsite_error=$1" "offsite_error_at=$(now_iso)"
    return 1
}

copy_offsite() {
    update_status "backup_dir=$BACKUP_DIR"
    if [[ -n "${OFFSITE_DIR:-}" ]]; then
        update_status "offsite_target=$OFFSITE_DIR"
        # A backup disk that isn't mounted leaves its folder (or the mount point) on this
        # server's own disk: copying there would look fine and protect nothing.
        local offsite_fs
        offsite_fs=$(fs_of "$OFFSITE_DIR")
        if [[ "$offsite_fs" == "$(fs_of "$BACKUP_DIR")" || "$offsite_fs" == "$(fs_of /)" ]]; then
            offsite_failed NOT_MOUNTED "$OFFSITE_DIR is on this server's own disk: is the backup disk mounted?"
            return
        fi
        mkdir -p "$OFFSITE_DIR"
        rsync -a --delete "$BACKUP_DIR"/ "$OFFSITE_DIR"/ \
            || { offsite_failed COPY_FAILED "rsync to $OFFSITE_DIR failed"; return; }
        update_status "offsite_copy_at=$(now_iso)" "offsite_error="
    elif [[ -n "${OFFSITE_RSYNC:-}" ]]; then
        update_status "offsite_target=$OFFSITE_RSYNC"
        rsync -a --delete -e "ssh -o BatchMode=yes" "$BACKUP_DIR"/ "$OFFSITE_RSYNC"/ \
            || { offsite_failed COPY_FAILED "rsync to $OFFSITE_RSYNC failed"; return; }
        update_status "offsite_copy_at=$(now_iso)" "offsite_error="
    else
        echo "WARNING: no OFFSITE_DIR/OFFSITE_RSYNC set: backups exist on this disk only." >&2
        update_status "offsite_target=" "offsite_error="
    fi
}
