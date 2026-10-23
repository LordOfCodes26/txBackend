#!/usr/bin/env bash
# Grist (install-grist.sh): archive its folder (documents, logins, settings) into
# $BACKUP_DIR/grist and check the archive by opening every database in it. Run by nightly.sh
# (pruned and copied off-site with the other backups); runs alone too, as root. Nothing to
# do when Grist isn't installed.
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/common.sh"

GRIST_DIR=${GRIST_DIR:-/opt/grist}
[[ -f "$GRIST_DIR/docker-compose.yml" ]] || { echo "No Grist in $GRIST_DIR: skipped."; exit 0; }

failed() {
    echo "GRIST BACKUP FAILED: $1" >&2
    update_status "grist_backup_error=$1" "grist_backup_error_at=$(now_iso)"
    exit 1
}

stamp=$(date -u +%Y%m%d-%H%M%S)
install -d -m 0750 "$BACKUP_DIR/grist"
archive="$BACKUP_DIR/grist/grist-$stamp.tar.gz"

echo "==> Archiving Grist ($GRIST_DIR)"
# Paused, Grist writes nothing while its SQLite files are copied (a few seconds; requests
# wait). Always unpaused, also when the copy fails.
paused=0
if docker inspect -f '{{.State.Running}}' grist 2>/dev/null | grep -qx true; then
    docker pause grist >/dev/null && paused=1
fi
unpause() { if (( paused )); then docker unpause grist >/dev/null; paused=0; fi; }
trap unpause EXIT
tar -C "$GRIST_DIR" -czf "$archive.partial" . || { rm -f "$archive.partial"; failed "tar failed"; }
unpause

echo "==> Checking the archive"
check=$(mktemp -d)
trap 'rm -rf "$check"; unpause' EXIT
tar -C "$check" -xzf "$archive.partial" || { rm -f "$archive.partial"; failed "the archive does not unpack"; }
if ! python3 - "$check" <<'PY'
import pathlib, sqlite3, sys
root = pathlib.Path(sys.argv[1])
files = [root / "persist" / "home.sqlite3", *sorted((root / "persist" / "docs").glob("*.grist"))]
bad = []
for path in files:
    if not path.exists():
        bad.append(f"{path.relative_to(root)} missing")
        continue
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        ok = db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.close()
    except sqlite3.DatabaseError:
        ok = False
    if not ok:
        bad.append(f"{path.relative_to(root)} damaged")
print(f"    {len(files) - 1} document(s) and the users database checked")
if bad:
    print("    " + "; ".join(bad), file=sys.stderr)
    sys.exit(1)
PY
then
    rm -f "$archive.partial"
    failed "the archive's databases are damaged"
fi
mv "$archive.partial" "$archive"
sha256sum "$archive" > "$archive.sha256"
update_status "last_grist_backup_at=$(now_iso)" "last_grist_backup_file=$(basename "$archive")" \
    "last_grist_backup_bytes=$(stat -c %s "$archive")" "grist_backup_error="
echo "Done: $archive"
