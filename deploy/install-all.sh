#!/usr/bin/env bash
# Install EVERYTHING on an offline Ubuntu 24.04 server from this kit folder: the backend, the
# frontend (Next.js) behind the same web address, and development copies of both projects
# with their full source, history and tools. No internet needed.
#
#   sudo bash install-all.sh                                  # asks what it needs
#   sudo bash install-all.sh --server-ip 192.168.1.10 --timezone Asia/Pyongyang \
#        --language ko --admin-email admin@chonha.com --dev-user kim
#
# Options:
#   --server-ip, --timezone, --language, --device-language, --hosts, --admin-email,
#   --no-admin, --yes          passed on to install-backend.sh (see its --help)
#   --dev-user NAME            set up the development copies for this user
#                              (default: the user running sudo; "none" = no development copies)
#   --seed-demo                fill the backend development copy with demo data
#   --skip-backend             don't (re)install the backend (e.g. only the frontend changed)
#   --frontend-from DIR        build and install the frontend from this folder (e.g. a
#                              developer's ~/frontend-dev) instead of the kit's package
#
# Re-running is safe: it upgrades what is installed and never overwrites developers' work.
set -euo pipefail

BACKEND_ARGS=()
DEV_USER="${SUDO_USER:-}"
SEED=0
SKIP_BACKEND=0
FRONTEND_FROM=""
ASSUME_YES=0

while (( $# )); do
    case "$1" in
        --server-ip|--timezone|--language|--device-language|--hosts|--admin-email)
            BACKEND_ARGS+=("$1" "$2"); shift 2 ;;
        --no-admin) BACKEND_ARGS+=("$1"); shift ;;
        --yes|-y) BACKEND_ARGS+=("--yes"); ASSUME_YES=1; shift ;;
        --dev-user) DEV_USER="$2"; shift 2 ;;
        --seed-demo) SEED=1; shift ;;
        --skip-backend) SKIP_BACKEND=1; shift ;;
        --frontend-from) FRONTEND_FROM="$(cd "$2" && pwd)"; shift 2 ;;
        -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { echo "    $*"; }
warn() { printf '    \033[33mWARNING: %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"
HERE=$(cd "$(dirname "$0")" && pwd)
read -r OS_ID OS_VERSION_ID < <(. /etc/os-release && echo "$ID $VERSION_ID")
[[ "$OS_ID" == ubuntu && "$OS_VERSION_ID" == 24.04 && "$(uname -m)" == x86_64 ]] \
    || die "This kit is for Ubuntu 24.04 x86_64 only."
[[ "$DEV_USER" != root ]] || DEV_USER=""   # sudo from a root shell: no developer known
if [[ "$DEV_USER" == none ]]; then DEV_USER=""; fi
if [[ -n "$DEV_USER" ]]; then id "$DEV_USER" >/dev/null 2>&1 || die "No such user: $DEV_USER"; fi

# ---------------------------------------------------------------------------------- kit files
check_sum() {  # check_sum FILE: verify FILE.sha256 when present
    if [[ -f "$1.sha256" ]]; then
        (cd "$(dirname "$1")" && sha256sum --check --quiet "$(basename "$1").sha256") \
            || die "Checksum mismatch: $(basename "$1") is damaged. Copy it again."
    fi
}
BACKEND_BUNDLE=$(ls -1t "$HERE"/backend-*.tar.gz 2>/dev/null | head -1 || true)
FRONTEND_PKG=$(ls -1t "$HERE"/management-app-offline*.tar.gz 2>/dev/null | head -1 || true)
say "Kit in $HERE"
(( SKIP_BACKEND )) || [[ -n "$BACKEND_BUNDLE" ]] || die "No backend-*.tar.gz in this folder."
[[ -n "$FRONTEND_PKG" ]] || die "No management-app-offline.tar.gz (frontend) in this folder."
for f in "$BACKEND_BUNDLE" "$FRONTEND_PKG"; do [[ -z "$f" ]] || check_sum "$f"; done
note "backend:  $(basename "${BACKEND_BUNDLE:-skipped}")"
note "frontend: $(basename "$FRONTEND_PKG")${FRONTEND_FROM:+ (building from $FRONTEND_FROM)}"
note "developer copies for: ${DEV_USER:-nobody}"
if (( ! ASSUME_YES )); then
    read -r -p "    Continue? (yes/no) [yes]: " answer </dev/tty || true
    [[ "${answer:-yes}" == yes ]] || die "Cancelled."
fi

# ---------------------------------------------------------------------------------- 1. backend
if (( SKIP_BACKEND )); then
    say "1. Backend: skipped (--skip-backend)"
    [[ -f /etc/backend/backend.env ]] || die "The backend isn't installed yet: run without --skip-backend."
else
    say "1. Backend"
    bash "$HERE/install-backend.sh" "${BACKEND_ARGS[@]}" "$BACKEND_BUNDLE"
fi
SERVER_IP=$(grep -s '^SERVER_IP=' /etc/backend/backend.env | cut -d= -f2- || true)
SERVER_IP=${SERVER_IP:-$(hostname -I | awk '{print $1}')}

# ---------------------------------------------------------------------------------- 2. node
say "2. Node.js (from the frontend package)"
WORK=/var/tmp/frontend-install
rm -rf "$WORK"
mkdir -p "$WORK"
note "unpacking the frontend package..."
tar -xzf "$FRONTEND_PKG" -C "$WORK"
NODE_TARBALL=$(ls -1 "$WORK"/node-v*-linux-x64.tar.xz 2>/dev/null | head -1 || true)
[[ -n "$NODE_TARBALL" ]] || die "The frontend package has no Node.js runtime (node-v*-linux-x64.tar.xz)."
[[ -d "$WORK/management-app" ]] || die "The frontend package has no management-app/ folder."
NODE_NAME=$(basename "$NODE_TARBALL" .tar.xz)
if [[ ! -x "/opt/node/$NODE_NAME/bin/node" ]]; then
    mkdir -p /opt/node
    tar -xJf "$NODE_TARBALL" -C /opt/node
fi
for cmd in node npm npx; do ln -sfn "/opt/node/$NODE_NAME/bin/$cmd" "/usr/local/bin/$cmd"; done
note "Node.js $(/usr/local/bin/node -v), npm $(/usr/local/bin/npm -v) in /opt/node/$NODE_NAME"

# ---------------------------------------------------------------------------------- 3. frontend
say "3. Frontend (build and service)"
SRC="${FRONTEND_FROM:-$WORK/management-app}"
[[ -f "$SRC/package.json" && -d "$SRC/node_modules" ]] \
    || die "$SRC is not a frontend folder with node_modules (package.json + node_modules)."
src_commit() {  # src_commit DIR: short commit of a git folder, also before git is installed
    if command -v git >/dev/null; then
        # safe.directory: the source may belong to a developer, and this runs as root.
        git -c safe.directory="*" -C "$1" rev-parse --short HEAD 2>/dev/null && return
    fi
    local head ref
    head=$(cat "$1/.git/HEAD" 2>/dev/null) || return 0
    if [[ "$head" == ref:* ]]; then
        ref=${head#ref: }
        if [[ -f "$1/.git/$ref" ]]; then cut -c1-7 "$1/.git/$ref"
        else grep -s " $ref\$" "$1/.git/packed-refs" | cut -c1-7; fi
    else
        echo "${head:0:7}"
    fi
}
COMMIT=$(src_commit "$SRC")
VERSION=${COMMIT:-build}-$(date +%Y%m%d%H%M%S)
RELEASE=/opt/frontend/releases/$VERSION
BUILD="$WORK/build"
id frontend >/dev/null 2>&1 || useradd --system --home-dir /opt/frontend --shell /usr/sbin/nologin frontend
mkdir -p /opt/frontend/releases "$BUILD"
note "copying the source..."
tar -C "$SRC" --exclude=./.next --exclude=./dist --exclude=./.env.local -cf - . | tar -C "$BUILD" -xf -
note "building (npm run build, offline; a few minutes)..."
(cd "$BUILD" && PATH="/opt/node/$NODE_NAME/bin:$PATH" NEXT_TELEMETRY_DISABLED=1 npm run build >"$WORK/build.log" 2>&1) \
    || { tail -30 "$WORK/build.log"; die "The frontend build failed (full log: $WORK/build.log)."; }
[[ -f "$BUILD/.next/standalone/server.js" ]] || die "No standalone server after the build: next.config needs output: \"standalone\"."
mkdir -p "$RELEASE"
cp -a "$BUILD/.next/standalone/." "$RELEASE/"
mkdir -p "$RELEASE/.next"
cp -a "$BUILD/.next/static" "$RELEASE/.next/static"
[[ ! -d "$BUILD/public" ]] || cp -a "$BUILD/public" "$RELEASE/public"
chown -R root:frontend "$RELEASE"
chmod -R g+rX,o-rwx "$RELEASE"
ln -sfn "$RELEASE" /opt/frontend/current
cat > /etc/systemd/system/frontend.service <<EOF
[Unit]
Description=Frontend (Next.js)
After=network.target backend-web.service

[Service]
User=frontend
Group=frontend
WorkingDirectory=/opt/frontend/current
Environment=NODE_ENV=production
Environment=NEXT_TELEMETRY_DISABLED=1
Environment=HOSTNAME=127.0.0.1
Environment=PORT=3100
# The backend, through nginx's local-only address (no TLS, keeps the browser's IP).
Environment=API_URL=http://127.0.0.1:8001
ExecStart=/opt/node/$NODE_NAME/bin/node server.js
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable frontend >/dev/null 2>&1
systemctl restart frontend
# "/" now serves the frontend; the backend keeps /api/v1, /admin, /ws, /static, /health, ...
install -m 644 /opt/backend/current/deploy/nginx/root-frontend.conf /etc/backend/nginx-root.conf
nginx -t 2>/dev/null || { nginx -t; die "nginx rejected the configuration."; }
systemctl reload nginx
# Keep the last 3 frontend releases.
ls -1dt /opt/frontend/releases/* | tail -n +4 | xargs -r rm -rf
note "frontend $VERSION running on 127.0.0.1:3100, published at https://$SERVER_IP/"

# ---------------------------------------------------------------------------------- 4. dev
if [[ -n "$DEV_USER" ]]; then
    say "4. Development copies for $DEV_USER"
    HOME_DIR=$(getent passwd "$DEV_USER" | cut -d: -f6)
    if (( SKIP_BACKEND )) && [[ -z "$BACKEND_BUNDLE" ]]; then
        note "backend copy: skipped (no backend bundle in the kit)"
    else
        SEED_ARG=(); (( SEED )) && SEED_ARG=(--seed-demo)
        bash "$HERE/setup-dev.sh" --user "$DEV_USER" --yes "${SEED_ARG[@]}" "$BACKEND_BUNDLE"
    fi
    FE_DEV="$HOME_DIR/frontend-dev"
    fe_git() { runuser -u "$DEV_USER" -- git -C "$FE_DEV" "$@"; }
    NEW_FE=0
    if [[ -n "$FRONTEND_FROM" ]]; then
        note "frontend copy: unchanged (building from --frontend-from)"
    elif [[ ! -e "$FE_DEV" ]]; then
        note "frontend copy: $FE_DEV (source, git history, node_modules)..."
        cp -a "$WORK/management-app" "$FE_DEV"
        # Point the development frontend at the development backend (runserver on :8000).
        printf '# Written by install-all.sh: the development backend (~/backend-dev, port 8000).\nAPI_URL=http://127.0.0.1:8000\n' \
            > "$FE_DEV/.env.local"
        chown -R "$DEV_USER:" "$FE_DEV"
        NEW_FE=1
    elif [[ ! -d "$FE_DEV/.git" ]]; then
        warn "$FE_DEV exists but is not a git repository: left alone"
    else
        note "frontend copy $FE_DEV exists: your work is kept (not overwritten)"
    fi
    # The kit's frontend history becomes the branch offline/main (as for the backend copy):
    # merge it when you're ready; your branches and files are never changed here.
    if [[ -z "$FRONTEND_FROM" && -d "$FE_DEV/.git" && -d "$WORK/management-app/.git" ]]; then
        FE_CACHE="$FE_DEV/.offline-cache/frontend.git"
        rm -rf "$FE_CACHE"
        mkdir -p "$FE_DEV/.offline-cache"
        git -c safe.directory="*" clone -q --bare --no-hardlinks "$WORK/management-app" "$FE_CACHE"
        chown -R "$DEV_USER:" "$FE_DEV/.offline-cache"
        grep -qx ".offline-cache/" "$FE_DEV/.git/info/exclude" 2>/dev/null \
            || echo ".offline-cache/" >> "$FE_DEV/.git/info/exclude"
        fe_git remote set-url offline "$FE_CACHE" 2>/dev/null || fe_git remote add offline "$FE_CACHE"
        fe_git fetch -q offline
        if (( NEW_FE )); then
            note "frontend copy ready ($(fe_git log --oneline -1))"
        else
            # The new kit's npm packages, for after the merge (offline there's no npm install):
            #   rm -rf node_modules && cp -a .offline-cache/node_modules .
            rsync -a --delete "$WORK/management-app/node_modules/" "$FE_DEV/.offline-cache/node_modules/"
            chown -R "$DEV_USER:" "$FE_DEV/.offline-cache/node_modules"
            behind=$(fe_git rev-list --count HEAD..offline/main 2>/dev/null || echo 0)
            note "the kit's frontend code is the branch offline/main ($behind new commits): merge it with  git merge offline/main"
            note "its npm packages are in .offline-cache/node_modules (see README, section 12 B)"
        fi
    fi
else
    say "4. Development copies: none (--dev-user)"
fi
rm -rf "$WORK"

# ---------------------------------------------------------------------------------- 5. checks
say "5. Checks"
FAILED=0
for _ in $(seq 1 30); do
    code=$(curl -sk -o /dev/null -w '%{http_code}' https://localhost/ || echo 000)
    [[ "$code" =~ ^(200|30[1278])$ ]] && break
    sleep 1
done
check() {  # check NAME URL
    local code
    code=$(curl -sk -o /dev/null -w '%{http_code}' "$2" || echo 000)
    printf '    %-34s %s\n' "$1" "$code"
    [[ "$code" =~ ^(200|30[1278])$ ]] || FAILED=1
}
printf '    %-34s %s\n' "frontend service" "$(systemctl is-active frontend || true)"
[[ "$(systemctl is-active frontend || true)" == active ]] || FAILED=1
check "https://localhost/ (frontend)" https://localhost/
check "https://localhost/health/ (backend)" https://localhost/health/
check "https://localhost/admin/ (backend)" https://localhost/admin/login/
check "http://127.0.0.1:8001/health/ (internal)" http://127.0.0.1:8001/health/
if (( FAILED )); then
    die "Something isn't answering (see above). Logs: journalctl -u frontend -u backend-web -n 100"
fi

cat <<EOF

$(printf '\033[32m')Everything is installed.$(printf '\033[0m')

  Open in a browser:   https://$SERVER_IP/          (the frontend)
  Backend admin:       https://$SERVER_IP/admin/
  API documentation:   https://$SERVER_IP/api/docs/   (sign in at /admin/ first)
  API base address:    https://$SERVER_IP/api/v1/

  Services: backend-web backend-ws backend-tcp backend-worker frontend nginx postgresql redis-server
EOF
if [[ -n "$DEV_USER" ]]; then
    cat <<EOF

  Development ($DEV_USER):
    ~/backend-dev    cd ~/backend-dev && .venv/bin/uvicorn config.asgi:application --reload --port 8000
    ~/frontend-dev   cd ~/frontend-dev && npm run dev        (http://127.0.0.1:3000)
EOF
fi
