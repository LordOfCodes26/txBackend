#!/usr/bin/env bash
# Restore from the Backups page. Started by backend-backup-admin as its own systemd job
# (the web app stops during the restore), as root:
#
#   restore_web.sh <dump file name in BACKUP_DIR/db> <username>
#
# Backs up the current data first (nightly.sh), then restore_dump.sh (check, keep the
# current database, restore, migrate, start). The outcome goes to the status file, which
# the page reads once the app is back.
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/common.sh"
name=${1:?usage: restore_web.sh <dump file name> <username>}
by=${2:-}
dump="$BACKUP_DIR/db/$name"

update_status "restore_running=true" "restore_file=$name" "restore_by=$by" \
    "restore_started_at=$(now_iso)" "restore_finished_at=" "restore_ok=" \
    "restore_kept_db=" "restore_error="
finish() {  # finish OK ERROR [KEPT]
    update_status "restore_running=false" "restore_ok=$1" "restore_error=$2" \
        "restore_kept_db=${3:-}" "restore_finished_at=$(now_iso)"
}

[[ -f "$dump" ]] || { finish false "The backup file is gone."; exit 1; }
echo "==> Backing up the current data first"
if ! "$DIR/nightly.sh"; then
    finish false "The backup of the current data failed, so nothing was restored."
    exit 1
fi

log=$(mktemp)
trap 'rm -f "$log"' EXIT
if "$DIR/restore_dump.sh" "$dump" --yes 2>&1 | tee "$log"; then
    finish true "" "$(grep -oP 'Kept the previous database as: \K\S+' "$log" || true)"
    exit 0
fi
kept=$(grep -oP 'Kept the previous database as: \K\S+' "$log" || true)
if [[ -z "$kept" ]]; then
    finish false "The backup file didn't pass its check, so nothing was changed."
else
    finish false "Restored, but the data doesn't fit this version (migration failed)." "$kept"
fi
exit 1
