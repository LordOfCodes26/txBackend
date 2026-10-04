#!/usr/bin/env bash
# Install or upgrade the backend on an OFFLINE Ubuntu 24.04 (x86_64) server.
#
# Put this script in the same folder as the bundle (backend-<version>.tar.gz and its
# .sha256 file) and run:
#
#   sudo bash install-backend.sh                         # newest bundle in this folder
#   sudo bash install-backend.sh backend-2026.10.03.tar.gz
#
# The script unpacks the .tar.gz itself (tar -xzf). To only unpack it, without installing:
#
#   bash install-backend.sh --extract-only               # into this folder
#   bash install-backend.sh --extract-only /opt/unpacked # into another folder
#
# Options:
#   --timezone Area/City     company timezone, e.g. Asia/Seoul (default: keep current, UTC on first install)
#   --language en|ko         language of the web/API when the browser doesn't choose one
#                            (ko = Korean, DPRK usage) (default: keep current, asked on first install)
#   --device-language en|ko  language on the door and till reader screens; use ko only if the
#                            readers can show Korean letters (default: keep current, asked on first install)
#   --server-ip IP           the server's address in the company network (e.g. 192.168.1.10);
#                            find it with: hostname -I (default: asked on first install)
#   --hosts "a,b"            extra host names / IPs clients use to reach the server
#                            (the server's own IPs and hostname are always allowed)
#   --admin-user NAME        create the first admin with this username (asks for the password)
#   --no-admin               don't create an admin account
#   --yes                    don't ask for confirmation
#   --extract-only [DIR]     check and unpack the bundle (default: next to this script), then stop
#
# No internet access is needed: the bundle contains the OS packages, Python packages and
# the application.
set -euo pipefail

TIMEZONE=""
LANGUAGE=""
SERVER_IP=""
DEVICE_LANGUAGE=""
EXTRA_HOSTS=""
ADMIN_USER=""
NO_ADMIN=0
ASSUME_YES=0
EXTRACT_ONLY=0
EXTRACT_DIR=""
BUNDLE_FILE=""

while (( $# )); do
    case "$1" in
        --timezone) TIMEZONE="$2"; shift 2 ;;
        --language) LANGUAGE="$2"; shift 2 ;;
        --server-ip) SERVER_IP="$2"; shift 2 ;;
        --device-language) DEVICE_LANGUAGE="$2"; shift 2 ;;
        --hosts) EXTRA_HOSTS="$2"; shift 2 ;;
        --admin-user) ADMIN_USER="$2"; shift 2 ;;
        --no-admin) NO_ADMIN=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --extract-only)
            EXTRACT_ONLY=1; shift
            if (( $# )) && [[ "$1" != -* && "$1" != *.tar.gz ]]; then EXTRACT_DIR="$1"; shift; fi ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; exit 2 ;;
        *) BUNDLE_FILE="$1"; shift ;;
    esac
done

lang_code() {  # lang_code VALUE -> en | ko-kp (or empty if unknown)
    case "${1,,}" in
        en|english) echo en ;;
        ko|ko-kp|kp|korean) echo ko-kp ;;
        *) echo "" ;;
    esac
}
for value in "$LANGUAGE" "$DEVICE_LANGUAGE"; do
    if [[ -n "$value" && -z "$(lang_code "$value")" ]]; then
        echo "Unknown language: $value (use en or ko)" >&2; exit 2
    fi
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ask() {  # ask "question" default -> echo answer
    local answer
    if (( ASSUME_YES )); then echo "$2"; return; fi
    read -r -p "$1 [$2]: " answer </dev/tty || true
    echo "${answer:-$2}"
}

HERE=$(cd "$(dirname "$0")" && pwd)
(( EXTRACT_ONLY )) || [[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

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

unpack() {  # unpack DIR: extract the bundle into DIR/$NAME
    mkdir -p "$1"
    rm -rf "${1:?}/$NAME"
    echo "    tar -xzf $(basename "$BUNDLE_FILE") -C $1"
    tar -xzf "$BUNDLE_FILE" -C "$1"
    echo "    unpacked: $1/$NAME ($(du -sh "$1/$NAME" | cut -f1))"
}

if (( EXTRACT_ONLY )); then
    say "Unpacking (no installation)"
    unpack "${EXTRACT_DIR:-$HERE}"
    echo "    To install from there: sudo $(cd "${EXTRACT_DIR:-$HERE}" && pwd)/$NAME/app/scripts/install_offline.sh"
    exit 0
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
say "Unpacking the bundle"
unpack "$WORK"

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
if [[ -z "$LANGUAGE" && $UPGRADE -eq 0 ]]; then
    LANGUAGE=$(ask "Default language for the web and API: en = English, ko = Korean" "en")
fi
if [[ -n "$LANGUAGE" ]]; then
    code=$(lang_code "$LANGUAGE"); [[ -n "$code" ]] || die "Unknown language: $LANGUAGE (use en or ko)"
    set_env LANGUAGE_CODE "$code"; CHANGED=1
fi
if [[ -z "$DEVICE_LANGUAGE" && $UPGRADE -eq 0 ]]; then
    DEVICE_LANGUAGE=$(ask "Language on door/till reader screens (ko only if they show Korean letters): en / ko" "en")
fi
if [[ -n "$DEVICE_LANGUAGE" ]]; then
    code=$(lang_code "$DEVICE_LANGUAGE"); [[ -n "$code" ]] || die "Unknown language: $DEVICE_LANGUAGE (use en or ko)"
    set_env DEVICE_LANGUAGE "$code"; CHANGED=1
fi
# Server IP: the address clients and doors use. Suggest the one the server's network uses.
SERVER_IPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+(\.[0-9]+){3}$' | grep -v '^127\.' || true)
CURRENT_SERVER_IP=$(grep '^SERVER_IP=' "$ENV_FILE" | cut -d= -f2- || true)
if [[ -z "$SERVER_IP" ]]; then
    if (( UPGRADE )) && [[ -n "$CURRENT_SERVER_IP" ]]; then
        SERVER_IP="$CURRENT_SERVER_IP"
    else
        SUGGESTED=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") {print $(i + 1); exit}}' || true)
        [[ -n "$SUGGESTED" ]] || SUGGESTED=$(echo "$SERVER_IPS" | head -1)
        echo "    this server's IP addresses: $(echo $SERVER_IPS)"
        SERVER_IP=$(ask "Server IP address that computers and doors will use" "${SUGGESTED:-127.0.0.1}")
    fi
fi
[[ "$SERVER_IP" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || die "Not an IPv4 address: $SERVER_IP (find it with: hostname -I)"
if ! grep -qx "$SERVER_IP" <<<"$SERVER_IPS"; then
    echo "    WARNING: $SERVER_IP is not an address of this server right now ($(echo $SERVER_IPS))."
    echo "             Computers and doors can't reach it until the server has that IP."
fi
if [[ "$SERVER_IP" != "$CURRENT_SERVER_IP" ]]; then set_env SERVER_IP "$SERVER_IP"; CHANGED=1; fi

# The installer's temporary self-signed certificate is made for the server IP. A certificate
# from the company's CA (not self-signed) is never touched.
CERT=/etc/backend/tls/cert.pem
if [[ -f "$CERT" ]] \
    && [[ "$(openssl x509 -in "$CERT" -noout -issuer | cut -d= -f2-)" == "$(openssl x509 -in "$CERT" -noout -subject | cut -d= -f2-)" ]] \
    && ! openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -qw "IP Address:$SERVER_IP"; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=$SERVER_IP" \
        -addext "subjectAltName=IP:$SERVER_IP,DNS:$(hostname),DNS:localhost,IP:127.0.0.1" \
        -keyout /etc/backend/tls/key.pem -out "$CERT" 2>/dev/null
    chmod 640 /etc/backend/tls/key.pem
    chown root:backend /etc/backend/tls/key.pem
    systemctl reload nginx
    echo "    temporary certificate made for $SERVER_IP (replace it with the company's one later)"
fi

HOSTS="localhost,127.0.0.1,$SERVER_IP,$(hostname),$(hostname -I | tr ' ' ',' | sed 's/,*$//')"
[[ -n "$EXTRA_HOSTS" ]] && HOSTS="$HOSTS,$EXTRA_HOSTS"
CURRENT_HOSTS=$(grep '^DJANGO_ALLOWED_HOSTS=' "$ENV_FILE" | cut -d= -f2-)
MERGED=$(printf '%s,%s' "$CURRENT_HOSTS" "$HOSTS" | tr ',' '\n' | sed '/^$/d' | awk '!seen[$0]++' | paste -sd, -)
if [[ "$MERGED" != "$CURRENT_HOSTS" ]]; then set_env DJANGO_ALLOWED_HOSTS "$MERGED"; CHANGED=1; fi
echo "    SERVER_IP=$SERVER_IP"
echo "    TIME_ZONE=$(grep '^TIME_ZONE=' "$ENV_FILE" | cut -d= -f2-)"
echo "    LANGUAGE_CODE=$(grep '^LANGUAGE_CODE=' "$ENV_FILE" | cut -d= -f2- || true)  (web/API default)"
echo "    DEVICE_LANGUAGE=$(grep '^DEVICE_LANGUAGE=' "$ENV_FILE" | cut -d= -f2- || true)  (reader screens)"
echo "    DJANGO_ALLOWED_HOSTS=$(grep '^DJANGO_ALLOWED_HOSTS=' "$ENV_FILE" | cut -d= -f2-)"
if (( CHANGED )); then
    systemctl restart backend-web backend-ws backend-tcp backend-worker
    sleep 3
fi

# ---------------------------------------------------------------------------- admin
HAS_ADMIN=$(manage shell -c "\"from apps.accounts.models import User; print(int(User.objects.filter(is_superuser=True, is_active=True).exists()))\"" 2>/dev/null | tail -1)
if [[ "$HAS_ADMIN" != "1" && $NO_ADMIN -eq 0 ]]; then
    say "First admin account"
    if [[ -z "$ADMIN_USER" ]]; then
        ADMIN_USER=$(ask "Admin username" "admin")
    fi
    if (( ASSUME_YES )) && [[ -z "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
        echo "    --yes without DJANGO_SUPERUSER_PASSWORD: create the admin later with:"
        echo "    sudo -u backend bash -c 'set -a; . $ENV_FILE; set +a; cd $APP && .venv/bin/python manage.py createsuperuser'"
    elif [[ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]]; then
        runuser -u backend -- env DJANGO_SUPERUSER_PASSWORD="$DJANGO_SUPERUSER_PASSWORD" bash -c \
            "set -a; . '$ENV_FILE'; set +a; cd '$APP' && .venv/bin/python manage.py createsuperuser --noinput --username '$ADMIN_USER'"
    else
        echo "    Choose a strong password (at least 8 characters, not only digits):"
        runuser -u backend -- bash -c \
            "set -a; . '$ENV_FILE'; set +a; cd '$APP' && .venv/bin/python manage.py createsuperuser --username '$ADMIN_USER'" </dev/tty
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
HTTPS_PORT=$(grep -s '^WEB_HTTPS_PORT=' "$ENV_FILE" | cut -d= -f2- || true); HTTPS_PORT=${HTTPS_PORT:-443}
for path in health/ health/db/ health/redis/ health/backup/; do
    code=$(curl -sk -o /dev/null -w '%{http_code}' "https://localhost:$HTTPS_PORT/$path" || echo 000)
    printf '    %-26s %s\n' "/$path" "$code"
    [[ "$code" == 200 ]] || FAILED=1
done
DOOR_PORT=$(grep -s '^RFID_TCP_PORT=' "$ENV_FILE" | cut -d= -f2- || true); DOOR_PORT=${DOOR_PORT:-9100}
if timeout 3 bash -c "</dev/tcp/127.0.0.1/$DOOR_PORT" 2>/dev/null; then
    printf '    %-26s %s\n' "door listener :$DOOR_PORT" "open"
else
    printf '    %-26s %s\n' "door listener :$DOOR_PORT" "CLOSED"; FAILED=1
fi

IP="$SERVER_IP"
[[ "$HTTPS_PORT" == 443 ]] || IP="$SERVER_IP:$HTTPS_PORT"   # the web address (with a changed port)
if (( FAILED )); then
    say "Installed $VERSION, but some checks failed (see above)."
    echo "    Logs: journalctl -u backend-web -u backend-tcp -u backend-ws -n 100"
    exit 1
fi

cat <<EOF

$(printf '\033[32m')Installed $VERSION successfully.$(printf '\033[0m')

  Web / API:     https://$IP/            (admin: https://$IP/admin/)
  API docs:      https://$IP/api/docs/   (sign in at https://$IP/admin/ first)
  Door devices:  TCP $SERVER_IP:$DOOR_PORT
  Till readers:  https://$IP/api/v1/rfid/events/
  Settings:      $ENV_FILE
  Backups:       /var/backups/backend   (config: /etc/backend/backup.conf)

Next steps (see docs/OFFLINE_DEPLOYMENT.md in $APP):
  1. Install your internal TLS certificate: /etc/backend/tls/cert.pem + key.pem, then
     systemctl reload nginx (the current one is self-signed).
  2. Set an off-site backup target (OFFSITE_DIR or OFFSITE_RSYNC) in /etc/backend/backup.conf.
  3. Create buildings, then register every door unit (code Door1, name Door1-1, its fixed
     IP) - journalctl -u backend-tcp shows "rejected ID='Door1' from <ip>" after a tap.
  4. Register the till readers (Reader1, ...) and card assign readers (Master1, ...).
  5. Firewall: allow 443 (and 80), and $DOOR_PORT only from the devices' network.
EOF
