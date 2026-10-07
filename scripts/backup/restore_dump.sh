#!/usr/bin/env bash
# Restore the database from a nightly dump.
#
#   sudo scripts/backup/restore_dump.sh /var/backups/backend/db/backend-<stamp>.dump --yes
#
# Stops the app, keeps the CURRENT database under a new name (never deleted by this
# script), restores the dump as the live database, migrates it to the installed code
# (an older dump has older tables) and starts the app again. backup.conf may set
# RESTORE_SERVICES, APP_DIR, ENV_FILE and APP_USER (staging uses other ones).
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/common.sh"
dump=${1:?usage: restore_dump.sh <dumpfile> --yes}
[[ "${2:-}" == "--yes" ]] || { echo "Refusing without --yes (this replaces the live database)." >&2; exit 1; }
DB_OWNER=${DB_OWNER:-$DB_NAME}
SERVICES=${SERVICES:-${RESTORE_SERVICES:-"backend-web backend-ws backend-tcp backend-worker"}}
APP_DIR=${APP_DIR:-/opt/backend/current}
ENV_FILE=${ENV_FILE:-/etc/backend/backend.env}
APP_USER=${APP_USER:-backend}

sha256sum --check --quiet "$dump.sha256"
echo "==> Checking the dump restores cleanly before touching the live database"
"$DIR/verify.sh" "$dump"

kept="${DB_NAME}_before_restore_$(date -u +%Y%m%d_%H%M%S)"
echo "==> Stopping: $SERVICES"
systemctl stop $SERVICES || true
echo "==> Keeping the current database as $kept"
as_postgres psql -v ON_ERROR_STOP=1 -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid();" >/dev/null
as_postgres psql -v ON_ERROR_STOP=1 -c "ALTER DATABASE \"$DB_NAME\" RENAME TO \"$kept\";"
echo "==> Restoring $dump"
as_postgres createdb --owner="$DB_OWNER" "$DB_NAME"
as_postgres pg_restore --no-owner --role="$DB_OWNER" --exit-on-error --dbname="$DB_NAME" "$dump"
migrated=1
if [[ -f "$APP_DIR/manage.py" ]]; then
    echo "==> Migrating the restored database to the installed code"
    runuser -u "$APP_USER" -- bash -c \
        "set -a; . '$ENV_FILE'; set +a; cd '$APP_DIR' && exec .venv/bin/python manage.py migrate --noinput" \
        || migrated=0
fi
echo "==> Starting: $SERVICES"
systemctl start $SERVICES || true
echo "Kept the previous database as: $kept"
if (( ! migrated )); then
    echo "MIGRATION FAILED: the restored data doesn't fit this version. To go back:" >&2
    echo "  stop the app, drop $DB_NAME, rename $kept to $DB_NAME, start the app." >&2
    exit 1
fi
echo "Restored. Drop the previous database once you're satisfied:"
echo "  sudo -u postgres dropdb $kept"
