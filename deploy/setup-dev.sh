#!/usr/bin/env bash
# Set up (or update) a DEVELOPMENT copy of the backend on the offline server, from the
# bundle next to this script. No internet needed. Separate from the installed backend:
# own folder, own database, own settings; the installed backend is not touched.
#
#   sudo bash setup-dev.sh                     # for the user who runs sudo, in ~/backend-dev
#   sudo bash setup-dev.sh --seed-demo         # ... and fill it with demo data
#
# Options:
#   --user NAME      whose development copy (default: the user running sudo)
#   --dir PATH       where (default: ~NAME/backend-dev)
#   --seed-demo      add demo data (people, stores, a month of attendance and purchases)
#   --run-tests      run the test suite at the end (about 3 minutes)
#   --yes            don't ask for confirmation
#
# Running it again with a newer bundle updates the copy: new packages are installed and
# the new code is fetched as the git branch offline/main (your own work is never changed:
# merge it yourself with  git merge offline/main).
set -euo pipefail

DEV_USER="${SUDO_USER:-}"
DEV_DIR=""
SEED=0
RUN_TESTS=0
ASSUME_YES=0
BUNDLE_FILE=""
DB_NAME=backend_dev
DB_USER=backend_dev
# A separate PostgreSQL instance for development: the installed backend's one archives every
# change into its backups, which demo data and test runs must not fill.
PG_CLUSTER=dev
PG_PORT=5433

while (( $# )); do
    case "$1" in
        --user) DEV_USER="$2"; shift 2 ;;
        --dir) DEV_DIR="$2"; shift 2 ;;
        --seed-demo) SEED=1; shift ;;
        --run-tests) RUN_TESTS=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
        *) BUNDLE_FILE="$1"; shift ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { echo "    $*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
as_user() { runuser -u "$DEV_USER" -- bash -c "cd '$DEV_DIR' && $*"; }

[[ $EUID -eq 0 ]] || die "Run with sudo: sudo bash $0"
HERE=$(cd "$(dirname "$0")" && pwd)

# ---------------------------------------------------------------------------------- who, where
[[ -n "$DEV_USER" ]] || die "Say whose copy it is: sudo bash $0 --user <name>"
id "$DEV_USER" >/dev/null 2>&1 || die "No such user: $DEV_USER"
[[ "$DEV_USER" != root ]] || die "Use a normal user account for development (--user <name>), not root."
HOME_DIR=$(getent passwd "$DEV_USER" | cut -d: -f6)
DEV_DIR="${DEV_DIR:-$HOME_DIR/backend-dev}"
CACHE="$DEV_DIR/.offline-cache"

# ---------------------------------------------------------------------------------- bundle
if [[ -z "$BUNDLE_FILE" ]]; then
    BUNDLE_FILE=$(ls -1t "$HERE"/backend-*.tar.gz 2>/dev/null | head -1 || true)
    [[ -n "$BUNDLE_FILE" ]] || die "No backend-*.tar.gz found next to this script."
fi
BUNDLE_FILE=$(cd "$(dirname "$BUNDLE_FILE")" && pwd)/$(basename "$BUNDLE_FILE")
NAME=$(basename "$BUNDLE_FILE" .tar.gz)
say "Development copy for $DEV_USER in $DEV_DIR, from $NAME"
if [[ -f "$BUNDLE_FILE.sha256" ]]; then
    (cd "$(dirname "$BUNDLE_FILE")" && sha256sum --check --quiet "$(basename "$BUNDLE_FILE").sha256") \
        || die "Checksum mismatch: copy the bundle again."
    note "checksum OK"
fi
read -r OS_ID OS_VERSION_ID < <(. /etc/os-release && echo "$ID $VERSION_ID")
[[ "$OS_ID" == ubuntu && "$OS_VERSION_ID" == 24.04 && "$(uname -m)" == x86_64 ]] \
    || die "This bundle is for Ubuntu 24.04 x86_64 only."
if (( ! ASSUME_YES )); then
    read -r -p "    Continue? (yes/no) [yes]: " answer </dev/tty || true
    [[ "${answer:-yes}" == yes ]] || die "Cancelled."
fi

WORK=/var/tmp/backend-dev-setup
rm -rf "${WORK:?}/$NAME"
mkdir -p "$WORK"
note "unpacking..."
tar -xzf "$BUNDLE_FILE" -C "$WORK"
BUNDLE="$WORK/$NAME"
[[ -f "$BUNDLE/backend.git-bundle" ]] || die "This bundle has no development files (built before 2026-10-02): use a newer one."

# ---------------------------------------------------------------------------------- OS packages
say "OS packages (from the bundle)"
# openssh-server: to reach the development servers from another PC (an SSH tunnel).
PKGS=(python3 python3-venv postgresql postgresql-contrib redis-server git gettext rsync openssl openssh-server)
APT_TMP=$(mktemp -d)
mkdir -p "$APT_TMP/sources.list.d"
echo "deb [trusted=yes] file:$BUNDLE/os-packages ./" > "$APT_TMP/sources.list"
APT_OPTS=(-o "Dir::Etc::SourceList=$APT_TMP/sources.list"
          -o "Dir::Etc::SourceParts=$APT_TMP/sources.list.d"
          -o "APT::Get::List-Cleanup=0")
apt-get "${APT_OPTS[@]}" update -qq
DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTS[@]}" install -y -q --no-install-recommends \
    "${PKGS[@]}" >/dev/null
rm -rf "$APT_TMP"
systemctl enable --now postgresql redis-server >/dev/null 2>&1
note "python3, PostgreSQL, Redis, git, gettext, SSH server: ready"

# ---------------------------------------------------------------------------------- source
say "Source code (git)"
NEW_REPO=0
if [[ ! -e "$DEV_DIR" ]]; then
    install -d -o "$DEV_USER" -g "$(id -gn "$DEV_USER")" "$DEV_DIR"
    BRANCH=$(git bundle list-heads "$BUNDLE/backend.git-bundle" | awk '{sub("refs/heads/", "", $2); print $2}' \
        | grep -x main || git bundle list-heads "$BUNDLE/backend.git-bundle" | awk '{sub("refs/heads/", "", $2); print $2; exit}')
    runuser -u "$DEV_USER" -- git clone -q -b "$BRANCH" -o offline "$BUNDLE/backend.git-bundle" "$DEV_DIR"
    NEW_REPO=1
    note "cloned: branch $BRANCH, $(as_user git rev-list --count HEAD) commits of history"
elif [[ -d "$DEV_DIR/.git" ]]; then
    note "existing copy: your work is kept"
else
    die "$DEV_DIR exists but is not a git repository: choose another place with --dir"
fi

# Keep the bundle's packages and history in the copy: later updates and offline builds use them.
install -d -o "$DEV_USER" "$CACHE"
rsync -a --delete "$BUNDLE/wheelhouse/" "$CACHE/wheelhouse/"
rsync -a --delete "$BUNDLE/os-packages/" "$CACHE/os-packages/"
cp "$BUNDLE/backend.git-bundle" "$CACHE/backend.git-bundle"
chown -R "$DEV_USER:" "$CACHE"
as_user "git remote set-url offline '$CACHE/backend.git-bundle' 2>/dev/null || git remote add offline '$CACHE/backend.git-bundle'"
as_user "git fetch -q offline"
if (( ! NEW_REPO )); then
    behind=$(as_user "git rev-list --count HEAD..offline/main 2>/dev/null || echo 0")
    note "the bundle's code is the branch offline/main ($behind new commits): merge it with  git merge offline/main"
fi
grep -qx ".offline-cache/" "$DEV_DIR/.git/info/exclude" 2>/dev/null \
    || echo ".offline-cache/" >> "$DEV_DIR/.git/info/exclude"

# ---------------------------------------------------------------------------------- python
say "Python environment with the development tools"
[[ -x "$DEV_DIR/.venv/bin/python" ]] || as_user "python3 -m venv .venv"
as_user ".venv/bin/pip install --quiet --no-index --find-links .offline-cache/wheelhouse \
    -r requirements/prod.txt -r requirements/dev.txt"
note "$(as_user ".venv/bin/python --version"), pytest, ruff, Django: installed"

# ---------------------------------------------------------------------------------- database
say "Development database ($DB_NAME, separate from the installed backend)"
PG_VERSION=$(ls /usr/lib/postgresql | sort -V | tail -1)
if ! pg_lsclusters -h | awk '{print $2}' | grep -qx "$PG_CLUSTER"; then
    pg_createcluster "$PG_VERSION" "$PG_CLUSTER" -p "$PG_PORT" --start >/dev/null
    note "own PostgreSQL $PG_VERSION instance '$PG_CLUSTER' on port $PG_PORT (no backups, local only)"
else
    pg_ctlcluster "$PG_VERSION" "$PG_CLUSTER" start 2>/dev/null || true
fi
psql_dev() { runuser -u postgres -- psql -p "$PG_PORT" "$@"; }
ENV_FILE="$DEV_DIR/.env"
role_exists=$(psql_dev -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'")
if [[ "$role_exists" == 1 && -f "$ENV_FILE" ]]; then
    note "database user $DB_USER exists; settings in $ENV_FILE kept"
else
    DB_PASSWORD=$(openssl rand -hex 16)
    if [[ "$role_exists" == 1 ]]; then
        psql_dev -qc "ALTER ROLE $DB_USER WITH LOGIN CREATEDB PASSWORD '$DB_PASSWORD'"
    else
        # CREATEDB: the tests create their own throw-away test database.
        psql_dev -qc "CREATE ROLE $DB_USER LOGIN CREATEDB PASSWORD '$DB_PASSWORD'"
    fi
fi
psql_dev -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1 \
    || runuser -u postgres -- createdb -p "$PG_PORT" -O "$DB_USER" "$DB_NAME"

if [[ ! -f "$ENV_FILE" ]]; then
    PROD_ENV=/etc/backend/backend.env
    get_prod() { grep -s "^$1=" "$PROD_ENV" | cut -d= -f2- || true; }
    TZ_VALUE=$(get_prod TIME_ZONE); TZ_VALUE=${TZ_VALUE:-$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)}
    LANG_VALUE=$(get_prod LANGUAGE_CODE)
    HOSTS="localhost,127.0.0.1,$(hostname),$(hostname -I | tr ' ' ',' | sed 's/,*$//')"
    sed -e "s|^DJANGO_SECRET_KEY=.*|DJANGO_SECRET_KEY=$(openssl rand -hex 50)|" \
        -e "s|^DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=$HOSTS|" \
        -e "s|^DATABASE_URL=.*|DATABASE_URL=postgres://$DB_USER:$DB_PASSWORD@localhost:$PG_PORT/$DB_NAME|" \
        -e "s|^REDIS_URL=.*|REDIS_URL=redis://localhost:6379/1|" \
        -e "s|^TIME_ZONE=.*|TIME_ZONE=$TZ_VALUE|" \
        -e "s|^LANGUAGE_CODE=.*|LANGUAGE_CODE=${LANG_VALUE:-en}|" \
        -e "s|^MEDIA_ROOT=.*|MEDIA_ROOT=$DEV_DIR/media|" \
        "$DEV_DIR/.env.example" > "$ENV_FILE"
    chown "$DEV_USER:" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    note "settings: $ENV_FILE (DEBUG on, its own database, Redis database 1)"
fi
as_user "mkdir -p media && .venv/bin/python manage.py migrate --noinput >/dev/null"
note "database ready: tables and roles created"

# ---------------------------------------------------------------------------------- extras
if (( SEED )); then
    say "Demo data"
    as_user ".venv/bin/python manage.py seed_demo && .venv/bin/python manage.py seed_more \
        && .venv/bin/python manage.py seed_attendance_month && .venv/bin/python manage.py seed_purchases_month"
fi
if (( RUN_TESTS )); then
    say "Tests"
    as_user ".venv/bin/pytest -q -p no:cacheprovider" || die "Some tests failed (see above)."
fi
rm -rf "${WORK:?}/$NAME"

say "Development copy ready"
cat <<EOF
    Folder:    $DEV_DIR   (git: $(as_user "git log --oneline -1"))
    Database:  $DB_NAME on the development PostgreSQL, port $PG_PORT   (settings: $ENV_FILE)

    As $DEV_USER:
      cd $DEV_DIR
      .venv/bin/uvicorn config.asgi:application --reload --port 8000   # try it: http://127.0.0.1:8000/admin/
      .venv/bin/python manage.py createsuperuser             # a login for this copy
      .venv/bin/pytest -q                                    # all tests
      .venv/bin/ruff check . && .venv/bin/ruff format .      # lint and format
      git add -A && git commit -m "..."                      # record your changes

    Make a new bundle from your committed changes, without internet, and install it:
      scripts/build_offline_bundle.sh --reuse .offline-cache 2026.10.06
      sudo bash dist/install-backend.sh
EOF
