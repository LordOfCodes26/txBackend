#!/usr/bin/env bash
# Install Grist (a spreadsheet in the browser) next to the app, and let the app copy its data
# into it every 10 minutes. Run it after install-all.sh, on the same server:
#
#   sudo bash install-grist.sh                     # grist-offline-kit.tar.gz in this folder
#   sudo bash install-grist.sh /path/to/grist-offline-kit.tar.gz
#
# Options:
#   --port PORT   HTTPS port people open Grist on (default 8444)
#   --yes         don't ask: a new Grist admin password is made up and printed
#
# It installs Docker and Grist from the Grist kit (its own install.sh), reachable only through
# this server's nginx at https://<server-ip>:8444 with the app's certificate. It makes the
# Grist login "app-sync" for the app, writes GRIST_* into /etc/backend/backend.env and runs the
# first sync. Run it again to repair, or with a newer Grist kit: documents and logins are kept.
set -euo pipefail

PORT=8444
ASSUME_YES=0
KIT_FILE=""
while (( $# )); do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; exit 2 ;;
        *) KIT_FILE="$1"; shift ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

ENV_FILE=/etc/backend/backend.env
APP=/opt/backend/current
GRIST=/opt/grist
LOG=/var/log/grist-install.log
HERE="$(cd "$(dirname "$0")" && pwd)"
KIT_FILE="${KIT_FILE:-$HERE/grist-offline-kit.tar.gz}"

[[ $EUID -eq 0 ]] || die "Run with sudo."
[[ -f "$ENV_FILE" && -d "$APP" ]] || die "Install the app first (install-all.sh)."
[[ -f "$KIT_FILE" ]] || die "Grist kit not found: $KIT_FILE"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "Not a port: $PORT"
if ss -ltnH "sport = :$PORT" | grep -q . && [[ ! -L /etc/nginx/sites-enabled/grist.conf ]]; then
    die "Port $PORT is in use. Choose another: --port 8445"
fi
SERVER_IP=$(grep -s '^SERVER_IP=' "$ENV_FILE" | cut -d= -f2- || true)
SERVER_IP=${SERVER_IP:-$(hostname -I | awk '{print $1}')}
PUBLIC_URL="https://${SERVER_IP}:${PORT}"

set_env() {  # set_env FILE KEY VALUE  (replace or append)
    if grep -q "^$2=" "$1"; then
        sed -i "s|^$2=.*|$2=$3|" "$1"
    else
        echo "$2=$3" >> "$1"
    fi
}

# The kit's folders must stay readable by its containers (nginx reads the passwords file).
umask 022
WORK=$(mktemp -d /var/tmp/grist-kit.XXXXXX)
trap 'rm -rf "$WORK"' EXIT

say "Checking and unpacking $(basename "$KIT_FILE")"
if [[ -f "$KIT_FILE.sha256" ]]; then
    (cd "$(dirname "$KIT_FILE")" && sha256sum -c --quiet "$(basename "$KIT_FILE").sha256") \
        || die "$(basename "$KIT_FILE") is damaged (its .sha256 does not match)."
fi
tar -xzf "$KIT_FILE" -C "$WORK"
KIT="$WORK/grist-offline-kit"
[[ -x "$KIT/install.sh" ]] || die "No grist-offline-kit/install.sh in $KIT_FILE"
(cd "$KIT" && sha256sum -c --quiet SHA256SUMS) || die "The Grist kit is damaged (SHA256SUMS)."

ADMIN_PASSWORD=""
if ! grep -q '^admin:' "$GRIST/auth/htpasswd" 2>/dev/null; then
    if (( ASSUME_YES )); then
        ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=')
        MADE_UP=1
    else
        echo "Choose a password for the Grist user \"admin\" (manages Grist's users)."
        read -rsp "Password: " ADMIN_PASSWORD </dev/tty; echo
        read -rsp "Repeat:   " again </dev/tty; echo
        [[ -n "$ADMIN_PASSWORD" && "$ADMIN_PASSWORD" == "$again" ]] || die "The passwords are empty or differ."
    fi
fi

say "Installing Docker and Grist (log: $LOG)"
# Grist's gate listens on this server only; people come in through nginx (TLS) below. The
# kit opens its port in ufw, which a local-only address can't be: a stand-in skips that.
mkdir -p "$WORK/bin"
printf '#!/bin/sh\necho "Status: inactive"\n' > "$WORK/bin/ufw"
chmod 755 "$WORK/bin/ufw"
if ! PATH="$WORK/bin:$PATH" GRIST_PASSWORD="$ADMIN_PASSWORD" "$KIT/install.sh" 127.0.0.1:8484 \
        >"$LOG" 2>&1 </dev/null; then
    tail -20 "$LOG"
    die "The Grist kit's installer failed (see $LOG)."
fi
# Links and the browser's connection use the address people open, not the local one.
if ! grep -qx "APP_HOME_URL=$PUBLIC_URL" "$GRIST/.env"; then
    set_env "$GRIST/.env" APP_HOME_URL "$PUBLIC_URL"
    (cd "$GRIST" && docker compose up -d >>"$LOG" 2>&1)
fi
echo "    Grist runs (cd $GRIST && sudo docker compose ps)"

say "Grist login for the app (app-sync)"
SYNC_PASSWORD=$(openssl rand -hex 24)
GRIST_PASSWORD="$SYNC_PASSWORD" grist-user add app-sync editor >/dev/null
set_env "$ENV_FILE" GRIST_URL http://127.0.0.1:8484
set_env "$ENV_FILE" GRIST_USER app-sync
set_env "$ENV_FILE" GRIST_PASSWORD "$SYNC_PASSWORD"
set_env "$ENV_FILE" GRIST_PUBLIC_URL "$PUBLIC_URL"
set_env "$ENV_FILE" GRIST_HTTPS_PORT "$PORT"
echo "    saved in $ENV_FILE (GRIST_*)"

say "HTTPS on port $PORT"
sed "s/listen 8444 ssl;/listen $PORT ssl;/" "$APP/deploy/nginx/grist.conf" \
    > /etc/nginx/sites-available/grist.conf
ln -sfn /etc/nginx/sites-available/grist.conf /etc/nginx/sites-enabled/grist.conf
nginx -t 2>/dev/null || { nginx -t; die "nginx rejected the configuration."; }
systemctl reload nginx
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow "$PORT/tcp" comment "Grist" >/dev/null
    echo "    firewall: port $PORT opened"
fi

say "First sync"
systemctl restart backend-web   # the Excel page's Grist card reads GRIST_*
systemctl enable --now backend-grist-sync.timer >/dev/null 2>&1 || true
if systemctl start backend-grist-sync.service; then
    journalctl -u backend-grist-sync.service -n 8 --no-pager -o cat | sed 's/^/    /'
else
    journalctl -u backend-grist-sync.service -n 20 --no-pager -o cat | sed 's/^/    /'
    die "The first sync failed (above). Fix it, then: sudo systemctl start backend-grist-sync"
fi

say "First Grist backup"
# The Backups page expects one from now on (the nightly backup makes the next ones).
"$APP/scripts/backup/grist_backup.sh" | sed 's/^/    /' \
    || echo "    WARNING: the Grist backup failed; the nightly backup tries again."

say "Done"
cat <<EOF
    Grist:           $PUBLIC_URL   (sign in as admin, then the document "Management data")
    Grist's users:   $PUBLIC_URL/manage-users/   (admin only: add people, passwords, roles)
    In the app:      Excel page -> Data in Grist (last sync, Sync now, Open in Grist)
    The data is copied every 10 minutes (systemctl list-timers backend-grist-sync.timer).
    Backups:         $GRIST is archived every night into /var/backups/backend/grist/.
EOF
if [[ -n "${MADE_UP:-}" ]]; then
    echo "    Grist admin password (write it down, it is not shown again): $ADMIN_PASSWORD"
fi
