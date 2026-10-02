#!/usr/bin/env bash
# Install or upgrade the backend on an offline server from a bundle made by
# build_offline_bundle.sh. Run as root from inside the extracted bundle:
#
#   tar -xzf backend-<version>.tar.gz && cd backend-<version> && sudo app/scripts/install_offline.sh
#
# The OS packages (Python venv, PostgreSQL, Redis, nginx, ...) are installed from the
# bundle's own os-packages/ repository; no internet access is used. The bundle is built
# for one OS release and CPU type (see BUILT_FOR); the script refuses any other.
#
# Each release goes to /opt/backend/releases/<version>; /opt/backend/current
# points at the active one, so rollback = repoint the symlink and restart.
set -euo pipefail

BUNDLE="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="$(cat "${BUNDLE}/VERSION")"
BASE=/opt/backend
RELEASE="${BASE}/releases/${VERSION}"
ETC=/etc/backend
ENV_FILE="${ETC}/backend.env"

[[ $EUID -eq 0 ]] || { echo "Run as root." >&2; exit 1; }

echo "==> Checking the OS matches the bundle"
# Read the OS details in a subshell: /etc/os-release defines VERSION, which would
# otherwise overwrite this release's VERSION.
read -r OS_ID OS_VERSION_ID OS_PRETTY < <(. /etc/os-release && echo "$ID $VERSION_ID $PRETTY_NAME")
THIS="${OS_PRETTY} $(uname -m)"
echo "    server: ${THIS}"
echo "    bundle: $(cat "${BUNDLE}/BUILT_FOR")"
if [[ "${OS_ID}" != "ubuntu" || "${OS_VERSION_ID}" != "24.04" || "$(uname -m)" != "x86_64" ]] \
        && [[ "${FORCE_OS:-}" != "1" ]]; then
    echo "This bundle is for Ubuntu 24.04 x86_64. Build one on a machine matching this server." >&2
    exit 1
fi

if [[ -d "${BUNDLE}/os-packages" ]]; then
    echo "==> Installing OS packages from the bundle (offline)"
    APT_TMP=$(mktemp -d)
    mkdir -p "${APT_TMP}/sources.list.d"
    echo "deb [trusted=yes] file:${BUNDLE}/os-packages ./" > "${APT_TMP}/sources.list"
    APT_OPTS=(-o "Dir::Etc::SourceList=${APT_TMP}/sources.list"
              -o "Dir::Etc::SourceParts=${APT_TMP}/sources.list.d"
              -o "APT::Get::List-Cleanup=0")
    apt-get "${APT_OPTS[@]}" update -qq
    DEBIAN_FRONTEND=noninteractive apt-get "${APT_OPTS[@]}" install -y -q --no-install-recommends \
        python3 python3-venv postgresql postgresql-contrib redis-server nginx openssl rsync \
        ca-certificates
    rm -rf "${APT_TMP}"
fi

for cmd in python3 psql redis-cli nginx openssl systemctl rsync; do
    command -v "$cmd" >/dev/null || { echo "Missing required command: $cmd" >&2; exit 1; }
done

echo "==> Service user and directories"
id backend >/dev/null 2>&1 || useradd --system --home-dir "${BASE}" --shell /usr/sbin/nologin backend
install -d -o root -g root -m 755 "${BASE}" "${BASE}/releases"
install -d -o root -g backend -m 750 "${ETC}" "${ETC}/tls"
install -d -o backend -g backend -m 750 /var/lib/backend /var/lib/backend/media

echo "==> Environment file"
if [[ ! -f "${ENV_FILE}" ]]; then
    DB_PASSWORD="$(openssl rand -hex 24)"
    SECRET_KEY="$(openssl rand -hex 50)"
    HOSTS="localhost,127.0.0.1,$(hostname),$(hostname -I | tr ' ' ',' | sed 's/,*$//')"
    sed -e "s/__SECRET_KEY__/${SECRET_KEY}/" \
        -e "s/__DB_PASSWORD__/${DB_PASSWORD}/" \
        -e "s/__ALLOWED_HOSTS__/${HOSTS}/" \
        "${BUNDLE}/app/deploy/backend.env.template" > "${ENV_FILE}"
    chown root:backend "${ENV_FILE}"
    chmod 640 "${ENV_FILE}"

    echo "==> Database (first install)"
    systemctl enable --now postgresql redis-server
    runuser -u postgres -- psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backend') THEN
        CREATE ROLE backend LOGIN PASSWORD '${DB_PASSWORD}';
    ELSE
        ALTER ROLE backend PASSWORD '${DB_PASSWORD}';
    END IF;
END \$\$;
SQL
    runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='backend'" | grep -q 1 \
        || runuser -u postgres -- createdb -O backend backend
fi

if [[ ! -f "${ETC}/tls/cert.pem" ]]; then
    echo "==> Self-signed TLS certificate (replace with one from your internal CA)"
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$(hostname)" \
        -keyout "${ETC}/tls/key.pem" -out "${ETC}/tls/cert.pem" 2>/dev/null
    chmod 640 "${ETC}/tls/key.pem"
    chown root:backend "${ETC}/tls/key.pem"
fi

echo "==> Release ${VERSION}"
rm -rf "${RELEASE}"
cp -a "${BUNDLE}/app" "${RELEASE}"
python3 -m venv "${RELEASE}/.venv"
"${RELEASE}/.venv/bin/pip" install --quiet --no-index --find-links "${BUNDLE}/wheelhouse" \
    -r "${RELEASE}/requirements/prod.txt"
chown -R backend:backend "${RELEASE}"

run_manage() {
    runuser -u backend -- bash -c \
        "set -a; . '${ENV_FILE}'; set +a; cd '${RELEASE}' && exec .venv/bin/python manage.py $*"
}
run_manage check --deploy --fail-level ERROR
run_manage migrate --noinput
run_manage collectstatic --noinput

echo "==> Activate"
ln -sfn "${RELEASE}" "${BASE}/current.new"
mv -T "${BASE}/current.new" "${BASE}/current"

install -m 644 "${RELEASE}/deploy/systemd/backend-web.service" /etc/systemd/system/
install -m 644 "${RELEASE}/deploy/systemd/backend-worker.service" /etc/systemd/system/
install -m 644 "${RELEASE}/deploy/systemd/backend-ws.service" /etc/systemd/system/
install -m 644 "${RELEASE}/deploy/systemd/backend-tcp.service" /etc/systemd/system/
for unit in backend-backup.service backend-backup.timer backend-basebackup.service backend-basebackup.timer; do
    install -m 644 "${RELEASE}/deploy/systemd/${unit}" /etc/systemd/system/
done
install -m 644 "${RELEASE}/deploy/nginx/backend.conf" /etc/nginx/sites-available/backend.conf
# What "/" serves: the backend, unless install-all.sh has put the frontend there (kept).
[[ -f "${ETC}/nginx-root.conf" ]] || install -m 644 "${RELEASE}/deploy/nginx/root-backend.conf" "${ETC}/nginx-root.conf"
ln -sfn /etc/nginx/sites-available/backend.conf /etc/nginx/sites-enabled/backend.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t

echo "==> Backups: WAL archiving, schedules, first base backup"
install -d -o postgres -g postgres -m 0750 /var/backups/backend /var/backups/backend/wal \
    /var/backups/backend/base /var/backups/backend/db /var/backups/backend/media
[[ -f "${ETC}/backup.conf" ]] || install -m 0644 "${RELEASE}/deploy/backup.conf.template" "${ETC}/backup.conf"
PG_CONF_D=$(ls -d /etc/postgresql/*/main/conf.d | sort -V | tail -1)
if ! cmp -s "${RELEASE}/deploy/postgres/90-backend-backup.conf" "${PG_CONF_D}/90-backend-backup.conf"; then
    install -m 0644 "${RELEASE}/deploy/postgres/90-backend-backup.conf" "${PG_CONF_D}/"
    systemctl restart postgresql   # archive_mode needs a restart
fi

systemctl daemon-reload
systemctl enable backend-web backend-worker backend-ws backend-tcp nginx redis-server postgresql
systemctl restart backend-web backend-worker backend-ws backend-tcp
systemctl reload nginx || systemctl restart nginx
systemctl enable --now backend-backup.timer backend-basebackup.timer
if ! ls -1d /var/backups/backend/base/*/ >/dev/null 2>&1; then
    # Point-in-time recovery needs a base backup to start from.
    "${RELEASE}/scripts/backup/base_backup.sh"
fi
if ! ls -1 /var/backups/backend/db/*.dump >/dev/null 2>&1; then
    # First dump + restore check now, so backups are proven (and /health/backup/ is
    # green) from day one instead of after the first night.
    "${RELEASE}/scripts/backup/nightly.sh"
fi
grep -Eq '^OFFSITE_(DIR|RSYNC)=.+' "${ETC}/backup.conf" || \
    echo "WARNING: set OFFSITE_DIR or OFFSITE_RSYNC in ${ETC}/backup.conf: backups are on this disk only."

echo "Installed ${VERSION}. Check: curl -k https://localhost/health/db/"
echo "Create the first admin: cd ${BASE}/current && sudo -u backend bash -c 'set -a; . ${ENV_FILE}; set +a; .venv/bin/python manage.py createsuperuser'"
