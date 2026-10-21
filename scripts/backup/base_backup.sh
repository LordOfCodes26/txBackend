#!/usr/bin/env bash
# Daily physical base backup for point-in-time recovery, then prune old base backups and
# the archived WAL only they needed. Run as root (systemd: backend-basebackup.service).
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/common.sh"

stamp=$(date -u +%Y%m%d-%H%M%S)
install -d -o postgres -g postgres -m 0750 "$BACKUP_DIR/base" "$BACKUP_DIR/wal"
target="$BACKUP_DIR/base/$stamp"
echo "==> Base backup to $target"
as_postgres pg_basebackup --pgdata="$target" --format=tar --gzip --checkpoint=fast \
    --wal-method=fetch --label="backend-$stamp"
update_status "last_base_backup_at=$(now_iso)" "last_base_backup=$stamp"

echo "==> Keeping the newest $KEEP_BASE_BACKUPS base backups"
mapfile -t all < <(ls -1d "$BACKUP_DIR"/base/*/ 2>/dev/null | sort)
if (( ${#all[@]} > KEEP_BASE_BACKUPS )); then
    for old in "${all[@]:0:${#all[@]}-KEEP_BASE_BACKUPS}"; do rm -rf "$old"; done
fi
prune_wal
free_space_guard
copy_offsite
echo "Done: $target"
