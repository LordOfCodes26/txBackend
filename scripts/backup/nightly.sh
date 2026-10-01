#!/usr/bin/env bash
# Nightly: full logical dump + media archive, verify by restoring, prune, copy off-site.
# Run as root (systemd: backend-backup.service).
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/common.sh"

stamp=$(date -u +%Y%m%d-%H%M%S)
install -d -o postgres -g postgres -m 0750 "$BACKUP_DIR" "$BACKUP_DIR/db" "$BACKUP_DIR/media" \
    "$BACKUP_DIR/wal" "$BACKUP_DIR/base"

echo "==> Dumping database $DB_NAME"
dump="$BACKUP_DIR/db/$DB_NAME-$stamp.dump"
as_postgres pg_dump --format=custom --compress=6 --file="$dump.partial" "$DB_NAME"
mv "$dump.partial" "$dump"
sha256sum "$dump" > "$dump.sha256"
update_status "last_dump_at=$(now_iso)" "last_dump_file=$(basename "$dump")" \
    "last_dump_bytes=$(stat -c %s "$dump")"

if [[ -n "${MEDIA_ROOT:-}" && -d "$MEDIA_ROOT" ]]; then
    echo "==> Archiving media"
    tar -C "$MEDIA_ROOT" -czf "$BACKUP_DIR/media/media-$stamp.tar.gz" .
fi

echo "==> Verifying the dump by restoring it"
if "$DIR/verify.sh" "$dump"; then
    update_status "last_verify_at=$(now_iso)" "last_verify_ok=true" \
        "last_verify_file=$(basename "$dump")"
else
    update_status "last_verify_at=$(now_iso)" "last_verify_ok=false" \
        "last_verify_file=$(basename "$dump")"
    echo "VERIFY FAILED for $dump" >&2
    exit 1
fi

echo "==> Pruning dumps older than $KEEP_DAILY_DAYS days"
find "$BACKUP_DIR/db" "$BACKUP_DIR/media" -type f -mtime "+$KEEP_DAILY_DAYS" -delete

echo "==> Copying off-site"
copy_offsite
echo "Done: $dump"
