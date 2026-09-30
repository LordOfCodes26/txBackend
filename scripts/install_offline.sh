#!/usr/bin/env bash
# Install or upgrade the backend on an offline server from a bundle made by
# build_offline_bundle.sh. Run as root from inside the extracted bundle:
#
#   tar -xzf backend-<version>.tar.gz && cd backend-<version> && sudo app/scripts/install_offline.sh
#
# Needs these OS packages already installed (from the Ubuntu install media or
# before the server went offline): python3-venv postgresql redis-server nginx openssl
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
for cmd in python3 psql redis-cli nginx openssl systemctl; do
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
install -m 644 "${RELEASE}/deploy/nginx/backend.conf" /etc/nginx/sites-available/backend.conf
ln -sfn /etc/nginx/sites-available/backend.conf /etc/nginx/sites-enabled/backend.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t

systemctl daemon-reload
systemctl enable backend-web backend-worker nginx redis-server postgresql
systemctl restart backend-web backend-worker
systemctl reload nginx || systemctl restart nginx

echo "Installed ${VERSION}. Check: curl -k https://localhost/health/db/"
echo "Create the first admin: cd ${BASE}/current && sudo -u backend bash -c 'set -a; . ${ENV_FILE}; set +a; .venv/bin/python manage.py createsuperuser'"
