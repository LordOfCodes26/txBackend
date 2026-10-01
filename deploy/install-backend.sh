#!/usr/bin/env bash
# Install or upgrade the backend on an OFFLINE Ubuntu 24.04 (x86_64) server.
#
# Put this script in the same folder as the bundle (backend-<version>.tar.gz and its
# .sha256 file) and run:
#
#   sudo bash install-backend.sh                         # newest bundle in this folder
#   sudo bash install-backend.sh backend-2026.10.03.tar.gz
#
# Options:
#   --timezone Area/City     company timezone, e.g. Asia/Seoul (default: keep current, UTC on first install)
#   --hosts "a,b"            extra host names / IPs clients use to reach the server
#                            (the server's own IPs and hostname are always allowed)
#   --admin-email EMAIL      create the first admin with this email (asks for the password)
#   --no-admin               don't create an admin account
#   --yes                    don't ask for confirmation
#
# No internet access is needed: the bundle contains the OS packages, Python packages and
# the application.
set -euo pipefail

TIMEZONE=""
EXTRA_HOSTS=""
ADMIN_EMAIL=""
NO_ADMIN=0
ASSUME_YES=0
BUNDLE_FILE=""

while (( $# )); do
    case "$1" in
        --timezone) TIMEZONE="$2"; shift 2 ;;
        --hosts) EXTRA_HOSTS="$2"; shift 2 ;;
        --admin-email) ADMIN_EMAIL="$2"; shift 2 ;;
        --no-admin) NO_ADMIN=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; exit 2 ;;
        *) BUNDLE_FILE="$1"; shift ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ask() {  # ask "question" default -> echo answer
    local answer
    if (( ASSUME_YES )); then echo "$2"; return; fi
    read -r -p "$1 [$2]: " answer </dev/tty || true
    echo "${answer:-$2}"
}

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"
HERE=$(cd "$(dirname "$0")" && pwd)

# ---------------------------------------------------------------------------- bundle
if [[ -z "$BUNDLE_FILE" ]]; then
    BUNDLE_FILE=$(ls -1t "$HERE"/backend-*.tar.gz 2>/dev/null | head -1 || true)
    [[ -n "$BUNDLE_FILE" ]] || die "No backend-*.tar.gz found next to this script."
fi
[[ -f "$BUNDLE_FILE" ]] || die "Bundle not found: $BUNDLE_FILE"
BUNDLE_FILE=$(cd "$(dirname "$BUNDLE_FILE")" && pwd)/$(basename "$BUNDLE_FILE")
NAME=$(basename "$BUNDLE_FILE" .tar.gz)
VERSION=${NAME#backend-}

say "Bundle: $NAME"
if [[ -f "$BUNDLE_FILE.sha256" ]]; then
    (cd "$(dirname "$BUNDLE_FILE")" && sha256sum --check --quiet "$(basename "$BUNDLE_FILE").sha256") \
        || die "Checksum mismatch: the file is damaged or incomplete. Copy it again."
    echo "    checksum OK"
else
    echo "    WARNING: no $NAME.tar.gz.sha256 next to the bundle; skipping the checksum check."
fi

# ---------------------------------------------------------------------------- OS
read -r OS_ID OS_VERSION_ID OS_PRETTY < <(. /etc/os-release && echo "$ID $VERSION_ID $PRETTY_NAME")
echo "    server: $OS_PRETTY $(uname -m)"
[[ "$OS_ID" == "ubuntu" && "$OS_VERSION_ID" == "24.04" && "$(uname -m)" == "x86_64" ]] \
    || die "This bundle is for Ubuntu 24.04 x86_64 only."

UPGRADE=0
[[ -f /etc/backend/backend.env ]] && UPGRADE=1
if (( UPGRADE )); then
    echo "    existing installation found: this is an UPGRADE (data and settings are kept)"
    echo "    current release: $(readlink /opt/backend/current 2>/dev/null || echo unknown)"
fi
[[ "$(ask "Continue installing $VERSION? (yes/no)" yes)" == yes ]] || die "Cancelled."

if (( UPGRADE )) && systemctl list-unit-files backend-backup.service >/dev/null 2>&1; then
    say "Safety backup before upgrading"
    systemctl start backend-backup.service || die "Pre-upgrade backup failed; not upgrading."
    echo "    backup done ($(ls -1t /var/backups/backend/db/*.dump | head -1))"
fi

# ---------------------------------------------------------------------------- install
WORK=/var/tmp/backend-install
say "Extracting to $WORK/$NAME"
rm -rf "${WORK:?}/$NAME"
mkdir -p "$WORK"
tar -xzf "$BUNDLE_FILE" -C "$WORK"

say "Running the offline installer (OS packages, database, services, backups)"
"$WORK/$NAME/app/scripts/install_offline.sh"

APP=/opt/backend/current
ENV_FILE=/etc/backend/backend.env
manage() {
    runuser -u backend -- bash -c "set -a; . '$ENV_FILE'; set +a; cd '$APP' && .venv/bin/python manage.py $*"
}
set_env() {  # set_env KEY VALUE  (replace or append in backend.env)
    if grep -q "^$1=" "$ENV_FILE"; then
        sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}

# ---------------------------------------------------------------------------- settings
say "Settings"
CHANGED=0
if [[ -z "$TIMEZONE" && $UPGRADE -eq 0 ]]; then
    TIMEZONE=$(ask "Company timezone (e.g. Asia/Seoul, Europe/Berlin)" "UTC")
fi
if [[ -n "$TIMEZONE" ]]; then
    [[ -f "/usr/share/zoneinfo/$TIMEZONE" ]] || die "Unknown timezone: $TIMEZONE"
    set_env TIME_ZONE "$TIMEZONE"; CHANGED=1
fi
HOSTS="localhost,127.0.0.1,$(hostname),$(hostname -I | tr ' ' ',' | sed 's/,*$//')"
[[ -n "$EXTRA_HOSTS" ]] && HOSTS="$HOSTS,$EXTRA_HOSTS"
CURRENT_HOSTS=$(grep '^DJANGO_ALLOWED_HOSTS=' "$ENV_FILE" | cut -d= -f2-)
MERGED=$(printf '%s,%s' "$CURRENT_HOSTS" "$HOSTS" | tr ',' '\n' | sed '/^$/d' | awk '!seen[$0]++' | paste -sd, -)
if [[ "$MERGED" != "$CURRENT_HOSTS" ]]; then set_env DJANGO_ALLOWED_HOSTS "$MERGED"; CHANGED=1; fi
echo "    TIME_ZONE=$(grep '^TIME_ZONE=' "$ENV_FILE" | cut -d= -f2-)"
echo "    DJANGO_ALLOWED_HOSTS=$(grep '^DJANGO_ALLOWED_HOSTS=' "$ENV_FILE" | cut -d= -f2-)"
if (( CHANGED )); then
    systemctl restart backend-web backend-ws backend-tcp backend-worker
    sleep 3
fi

# ---------------------------------------------------------------------------- admin
HAS_ADMIN=$(manage shell -c "\"from apps.accounts.models import User; print(int(User.objects.filter(is_superuser=True, is_active=True).exists()))\"" 2>/dev/null | tail -1)
if [[ "$HAS_ADMIN" != "1" && $NO_ADMIN -eq 0 ]]; then
    say "First admin account"
    if [[ -z "$ADMIN_EMAIL" ]]; then
        ADMIN_EMAIL=$(ask "Admin email" "admin@example.com")
    fi
    if (( ASSUME_YES )) && [[ -z "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
        echo "    --yes without DJANGO_SUPERUSER_PASSWORD: create the admin later with:"
        echo "    sudo -u backend bash -c 'set -a; . $ENV_FILE; set +a; cd $APP && .venv/bin/python manage.py createsuperuser'"
    elif [[ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
        runuser -u backend -- env DJANGO_SUPERUSER_PASSWORD="$DJANGO_SUPERUSER_PASSWORD" bash -c \
            "set -a; . '$ENV_FILE'; set +a; cd '$APP' && .venv/bin/python manage.py createsuperuser --noinput --email '$ADMIN_EMAIL'"
    else
        echo "    Choose a strong password (at least 8 characters, not only digits):"
        runuser -u backend -- bash -c \
            "set -a; . '$ENV_FILE'; set +a; cd '$APP' && .venv/bin/python manage.py createsuperuser --email '$ADMIN_EMAIL'" </dev/tty
    fi
elif [[ "$HAS_ADMIN" == "1" ]]; then
    echo "    An admin account already exists; skipping."
fi

# ---------------------------------------------------------------------------- checks
say "Health checks"
FAILED=0
for svc in postgresql redis-server nginx backend-web backend-ws backend-tcp backend-worker \
           backend-backup.timer backend-basebackup.timer; do
    state=$(systemctl is-active "$svc" || true)
    printf '    %-26s %s\n' "$svc" "$state"
    [[ "$state" == active ]] || FAILED=1
done
for path in health/ health/db/ health/redis/ health/backup/; do
    code=$(curl -sk -o /dev/null -w '%{http_code}' "https://localhost/$path" || echo 000)
    printf '    %-26s %s\n' "/$path" "$code"
    [[ "$code" == 200 ]] || FAILED=1
done
if timeout 3 bash -c '</dev/tcp/127.0.0.1/9100' 2>/dev/null; then
    printf '    %-26s %s\n' "door listener :9100" "open"
else
    printf '    %-26s %s\n' "door listener :9100" "CLOSED"; FAILED=1
fi

IP=$(hostname -I | awk '{print $1}')
if (( FAILED )); then
    say "Installed $VERSION, but some checks failed (see above)."
    echo "    Logs: journalctl -u backend-web -u backend-tcp -u backend-ws -n 100"
    exit 1
fi

cat <<EOF

$(printf '\033[32m')Installed $VERSION successfully.$(printf '\033[0m')

  Web / API:     https://$IP/            (admin: https://$IP/admin/)
  API docs:      https://$IP/api/docs/   (after logging in)
  Door devices:  TCP $IP:9100
  Till readers:  https://$IP/api/v1/rfid/events/
  Settings:      $ENV_FILE
  Backups:       /var/backups/backend   (config: /etc/backend/backup.conf)

Next steps (see docs/OFFLINE_DEPLOYMENT.md in $APP):
  1. Install your internal TLS certificate: /etc/backend/tls/cert.pem + key.pem, then
     systemctl reload nginx (the current one is self-signed).
  2. Set an off-site backup target (OFFSITE_DIR or OFFSITE_RSYNC) in /etc/backend/backup.conf.
  3. Create buildings, then register the doors (Door1, Door2) and set each door's IP
     (journalctl -u backend-tcp shows "rejected ID='Door1' from <ip>" after a tap).
  4. Register the till readers (Reader1, ...) with their serial numbers.
  5. Firewall: allow 443 (and 80), and 9100 only from the doors.
EOF
