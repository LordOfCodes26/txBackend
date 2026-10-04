#!/usr/bin/env bash
# mgmt: everyday operations for the management system installed by the offline kit
# (Ubuntu 24.04). Installed as /usr/local/sbin/mgmt (a link to the running release).
#
#   sudo mgmt deploy-backend   [--user NAME] [--yes]   put ~/backend-dev's last commit live
#   sudo mgmt deploy-frontend  [--user NAME] [--yes]   put ~/frontend-dev's last commit live
#   sudo mgmt deploy-all       [--user NAME] [--yes]   both
#   sudo mgmt change-door-port PORT                    the devices' TCP port (default 9100)
#   sudo mgmt change-web-port  HTTP HTTPS              the web ports (default 80 443)
#   sudo mgmt uninstall        [--remove-data] [--yes] remove it (data and firewall kept unless --remove-data)
#   sudo mgmt status                                   services, ports, health
#
# Deploys take COMMITTED code only. Each one is a new release next to the running one; if
# it doesn't start, the previous one is put back. Port changes are saved in backend.env
# (upgrades keep them) and put back if the new port doesn't answer.
set -euo pipefail

BASE=/opt/backend
FRONT=/opt/frontend
ETC=/etc/backend
ENV_FILE=$ETC/backend.env
NGINX_SITE=/etc/nginx/sites-available/backend.conf
BACKEND_SERVICES=(backend-web backend-ws backend-tcp backend-worker)
OWN_PORTS=(5432 5433 6379 8001 8091 3100)   # PostgreSQL, Redis, nginx-internal, ws, frontend

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { echo "    $*"; }
warn() { printf '    \033[33mWARNING: %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
usage() { sed -n '2,16p' "$(readlink -f "$0")" | sed 's/^# \{0,1\}//'; }

env_get() { grep -s "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }
env_set() {  # env_set KEY VALUE: replace or append in backend.env
    if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >> "$ENV_FILE"; fi
}
door_port() { local p; p=$(env_get RFID_TCP_PORT); echo "${p:-9100}"; }
http_port() { local p; p=$(env_get WEB_HTTP_PORT); echo "${p:-80}"; }
https_port() { local p; p=$(env_get WEB_HTTPS_PORT); echo "${p:-443}"; }
https_base() { local p; p=$(https_port); if [[ "$p" == 443 ]]; then echo https://localhost; else echo "https://localhost:$p"; fi; }
http_code() { curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$1" 2>/dev/null || echo 000; }
port_open() { timeout 2 bash -c "</dev/tcp/127.0.0.1/$1" 2>/dev/null; }
wait_port() { for _ in $(seq 1 "${2:-30}"); do port_open "$1" && return 0; sleep 1; done; return 1; }
wait_ok() {  # wait_ok URL [SECONDS]
    local code=000
    for _ in $(seq 1 "${2:-30}"); do
        code=$(http_code "$1"); [[ "$code" =~ ^(200|30[1278])$ ]] && return 0; sleep 1
    done
    return 1
}
valid_port() { [[ "$1" =~ ^[0-9]+$ ]] && (( $1 >= 1 && $1 <= 65535 )); }
port_owner() { ss -Hltnp "sport = :$1" 2>/dev/null | grep -oP 'users:\(\("\K[^"]+' | head -1 || true; }   # empty when free
confirm() {  # confirm QUESTION DEFAULT
    (( ASSUME_YES )) && return 0
    local answer; read -r -p "    $1 (yes/no) [$2]: " answer </dev/tty 2>/dev/null || true
    [[ "${answer:-$2}" == yes ]]
}
need_root() { [[ $EUID -eq 0 ]] || die "Run with sudo: sudo mgmt $CMD"; }
need_installed() { [[ -f "$ENV_FILE" ]] || die "The system isn't installed ($ENV_FILE is missing)."; }
manage() {  # manage RELEASE ARGS...: manage.py as the backend user with the live settings
    local release=$1; shift
    runuser -u backend -- bash -c "set -a; . '$ENV_FILE'; set +a; cd '$release' && .venv/bin/python manage.py $*"
}
keep_last_3() {  # keep_last_3 RELEASES_DIR CURRENT_LINK
    local active; active=$(readlink -f "$2")
    ls -1dt "$1"/*/ 2>/dev/null | tail -n +4 | while read -r d; do
        [[ "$(readlink -f "$d")" == "$active" ]] || rm -rf "$d"
    done
}
switch_link() { ln -sfn "$2" "$1.new"; mv -T "$1.new" "$1"; }
# A release that didn't go live (a failed build, check, migration or start) is removed on exit.
UNUSED_RELEASE=""
trap '[[ -z "$UNUSED_RELEASE" || ! -d "$UNUSED_RELEASE" ]] || rm -rf "$UNUSED_RELEASE"' EXIT

# ----------------------------------------------------------------------------- development copies
dev_home() {
    [[ -n "$DEV_USER" && "$DEV_USER" != root ]] || die "Say whose copies to deploy: sudo mgmt $CMD --user NAME"
    id "$DEV_USER" >/dev/null 2>&1 || die "No such user: $DEV_USER"
    getent passwd "$DEV_USER" | cut -d: -f6
}
dev_git() { local dir=$1; shift; git -c safe.directory='*' -C "$dir" "$@"; }
commit_of() {  # commit_of DIR LABEL: prints the commit to deploy (messages go to stderr)
    local dir=$1 dirty
    [[ -d "$dir/.git" ]] || die "$dir is not a development copy (no git repository)."
    {
        note "$2: $(dev_git "$dir" log -1 --format='%h  %s')"
        dirty=$(dev_git "$dir" status --porcelain --untracked-files=no)
        if [[ -n "$dirty" ]]; then
            warn "$(wc -l <<<"$dirty") changed file(s) in $dir are NOT committed and will NOT be deployed:"
            head -10 <<<"$dirty" | sed 's/^/        /'
            confirm "Deploy the last commit anyway?" no \
                || die "Cancelled: commit your changes (git add -A && git commit -m \"...\"), then deploy again."
        fi
    } >&2
    dev_git "$dir" rev-parse --short HEAD
}

deploy_backend() {
    local home dev commit release previous stamp
    home=$(dev_home); dev="$home/backend-dev"
    say "Backend from $dev"
    commit=$(commit_of "$dev" commit)
    [[ -d "$dev/.offline-cache/wheelhouse" ]] || die "No $dev/.offline-cache/wheelhouse: set the copy up with install-all.sh first."
    if systemctl list-unit-files backend-backup.service >/dev/null 2>&1; then
        note "safety backup..."
        systemctl start backend-backup.service || die "The safety backup failed; nothing was changed."
    fi
    stamp=$(date +%Y%m%d%H%M%S)
    release="$BASE/releases/dev-$commit-$stamp"
    note "release $(basename "$release")..."
    mkdir -p "$release"
    UNUSED_RELEASE=$release
    dev_git "$dev" archive --format=tar HEAD | tar -xf - -C "$release"
    python3 -m venv "$release/.venv"
    if ! "$release/.venv/bin/pip" install --quiet --disable-pip-version-check --no-index \
            --find-links "$dev/.offline-cache/wheelhouse" -r "$release/requirements/prod.txt"; then
        die "A Python package the code needs isn't in the offline wheelhouse: new packages need a kit built with internet."
    fi
    chown -R backend:backend "$release"
    manage "$release" check --deploy --fail-level ERROR >/dev/null
    note "database migrations..."
    manage "$release" migrate --noinput
    manage "$release" collectstatic --noinput >/dev/null
    previous=$(readlink -f "$BASE/current")
    switch_link "$BASE/current" "$release"
    systemctl restart "${BACKEND_SERVICES[@]}"
    if ! wait_ok http://127.0.0.1:8001/health/ 40 || ! wait_port "$(door_port)" 20; then
        warn "The new release didn't start: going back to $(basename "$previous")"
        switch_link "$BASE/current" "$previous"
        systemctl restart "${BACKEND_SERVICES[@]}"
        die "Deploy failed (the previous release is running again). Logs: journalctl -u backend-web -u backend-tcp -n 80"
    fi
    UNUSED_RELEASE=""
    keep_last_3 "$BASE/releases" "$BASE/current"
    note "backend $commit is live"
}

deploy_frontend() {
    local home dev commit build release previous node_bin stamp
    home=$(dev_home); dev="$home/frontend-dev"
    say "Frontend from $dev"
    commit=$(commit_of "$dev" commit)
    [[ -d "$dev/node_modules/next" ]] || die "$dev has no node_modules."
    node_bin=$(dirname "$(readlink -f /usr/local/bin/node)")
    [[ -x "$node_bin/node" ]] || die "Node.js isn't installed (/usr/local/bin/node)."
    build=/var/tmp/frontend-deploy
    rm -rf "$build"; mkdir -p "$build"
    dev_git "$dev" archive --format=tar HEAD | tar -xf - -C "$build"
    note "copying node_modules..."
    cp -a "$dev/node_modules" "$build/node_modules"
    note "building (next build; a few minutes)..."
    (cd "$build" && PATH="$node_bin:$PATH" NEXT_TELEMETRY_DISABLED=1 NODE_ENV=production \
        node node_modules/next/dist/bin/next build >"$build.log" 2>&1) \
        || { tail -30 "$build.log"; die "The build failed (full log: $build.log); nothing was changed."; }
    [[ -f "$build/.next/standalone/server.js" ]] || die "The build made no standalone server."
    stamp=$(date +%Y%m%d%H%M%S)
    release="$FRONT/releases/dev-$commit-$stamp"
    mkdir -p "$release/.next"
    UNUSED_RELEASE=$release
    cp -a "$build/.next/standalone/." "$release/"
    cp -a "$build/.next/static" "$release/.next/static"
    [[ ! -d "$build/public" ]] || cp -a "$build/public" "$release/public"
    chown -R root:frontend "$release"
    chmod -R g+rX,o-rwx "$release"
    rm -rf "$build"
    previous=$(readlink -f "$FRONT/current")
    switch_link "$FRONT/current" "$release"
    systemctl restart frontend
    if ! wait_ok http://127.0.0.1:3100/ 40; then
        warn "The new release didn't start: going back to $(basename "$previous")"
        switch_link "$FRONT/current" "$previous"
        systemctl restart frontend
        die "Deploy failed (the previous release is running again). Logs: journalctl -u frontend -n 80"
    fi
    UNUSED_RELEASE=""
    keep_last_3 "$FRONT/releases" "$FRONT/current"
    note "frontend $commit is live"
}

deploy_checks() {
    say "Checks"
    local failed=0 url code
    for url in "$(https_base)/" "$(https_base)/health/" "$(https_base)/health/db/"; do
        wait_ok "$url" 20 || true
        code=$(http_code "$url"); printf '    %-34s %s\n' "$url" "$code"
        [[ "$code" =~ ^(200|30[1278])$ ]] || failed=1
    done
    (( ! failed )) || die "Deployed, but something isn't answering (see above)."
    printf '\n\033[32mDeployed.\033[0m\n'
}

# ----------------------------------------------------------------------------- firewall (ufw)
ufw_active() { command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; }
ufw_rules_for() {  # ufw_rules_for PORT: the "ufw allow ..." rules (as added) for this TCP port
    ufw show added 2>/dev/null | grep -E "^ufw allow " \
        | grep -E "(^ufw allow $1/tcp|port $1 proto tcp)( |$)" || true
}
ufw_delete() {  # ufw_delete "ufw allow ... [comment '...']": delete that rule
    local rule=${1% comment *}
    eval "${rule/ufw allow/ufw --force delete allow}" >/dev/null
}
ufw_move() {  # ufw_move OLD NEW: re-create OLD's allow rules for NEW (who may use it stays)
    local old=$1 new=$2 rule
    ufw_active || { note "firewall (ufw) not active: nothing to change"; return 0; }
    local rules; rules=$(ufw_rules_for "$old")
    [[ -n "$rules" ]] || { warn "no ufw rule for port $old: allowing $new from anywhere"; ufw allow "$new/tcp" >/dev/null; return 0; }
    while read -r rule; do
        [[ -n "$rule" ]] || continue
        ufw_delete "$rule"
        rule=${rule//"allow $old/tcp"/"allow $new/tcp"}
        rule=${rule//"port $old proto tcp"/"port $new proto tcp"}
        eval "$rule" >/dev/null
    done <<<"$rules"
    note "firewall: port $old's rules now for $new"
}

# ----------------------------------------------------------------------------- ports
change_door_port() {
    local new=${ARGS[0]:-} old
    old=$(door_port)
    say "Door port: now $old"
    if [[ -z "$new" ]]; then read -r -p "    New port for the door and reader devices [$old]: " new </dev/tty 2>/dev/null || true; new=${new:-$old}; fi
    valid_port "$new" || die "Not a port: $new"
    [[ "$new" != "$old" ]] || { note "Already $new: nothing to change."; return 0; }
    [[ " ${OWN_PORTS[*]} $(http_port) $(https_port) " != *" $new "* ]] || die "Port $new is used by this system itself; choose another (e.g. 9200)."
    [[ -z "$(port_owner "$new")" ]] || die "Port $new is already used by '$(port_owner "$new")'; choose another."
    env_set RFID_TCP_PORT "$new"
    systemctl restart backend-tcp
    if ! wait_port "$new" 30; then
        warn "The door listener didn't come up on $new: going back to $old"
        env_set RFID_TCP_PORT "$old"; systemctl restart backend-tcp
        die "Port not changed. Logs: journalctl -u backend-tcp -n 50"
    fi
    ufw_move "$old" "$new"
    note "RFID_TCP_PORT=$new in $ENV_FILE; the door listener answers on $new"
    printf '\n\033[32mDone.\033[0m Now set every door and reader device to send to %s:%s\n' "$(env_get SERVER_IP)" "$new"
    echo "(until then they still send to the old port and are not heard)."
}

apply_web_ports() {  # apply_web_ports HTTP HTTPS: put the ports into nginx's site (no reload)
    local http=$1 https=$2 target='https://$host$request_uri'
    [[ "$https" == 443 ]] || target="https://\$host:$https\$request_uri"
    sed -i -E \
        -e "s|^(\s*)listen [0-9]+ default_server;|\1listen $http default_server;|" \
        -e "s|^(\s*)listen [0-9]+ ssl default_server;|\1listen $https ssl default_server;|" \
        -e "s|^(\s*)return 301 https://\\\$host(:[0-9]+)?\\\$request_uri;|\1return 301 $target;|" \
        "$NGINX_SITE"
}

change_web_port() {
    local http=${ARGS[0]:-} https=${ARGS[1]:-} old_http old_https p
    old_http=$(http_port); old_https=$(https_port)
    say "Web ports: now HTTP $old_http, HTTPS $old_https"
    if [[ -z "$http" && -z "$https" ]]; then
        read -r -p "    New HTTPS port (what browsers and till programs use) [$old_https]: " https </dev/tty 2>/dev/null || true
        read -r -p "    New HTTP port (only redirects to HTTPS) [$old_http]: " http </dev/tty 2>/dev/null || true
        http=${http:-$old_http}; https=${https:-$old_https}
    elif [[ -z "$http" || -z "$https" ]]; then
        die "Give both ports, HTTP first: sudo mgmt change-web-port 8080 8443"
    fi
    valid_port "$http" && valid_port "$https" || die "Ports are numbers, e.g. 8080 8443."
    [[ "$http" != "$https" ]] || die "The HTTP and HTTPS ports must differ."
    [[ "$http" != "$old_http" || "$https" != "$old_https" ]] || { note "Already these ports: nothing to change."; return 0; }
    for p in "$http" "$https"; do
        [[ " ${OWN_PORTS[*]} $(door_port) " != *" $p "* ]] || die "Port $p is used by this system itself; choose another."
        local owner; owner=$(port_owner "$p")
        [[ -z "$owner" || "$owner" == nginx ]] || die "Port $p is already used by '$owner'; choose another."
    done
    cp -a "$NGINX_SITE" "$NGINX_SITE.before-port-change"
    env_set WEB_HTTP_PORT "$http"; env_set WEB_HTTPS_PORT "$https"
    apply_web_ports "$http" "$https"
    if ! nginx -t >/dev/null 2>&1 || ! systemctl reload nginx || ! wait_ok "https://localhost:$https/health/" 20; then
        warn "The new ports didn't work: going back to $old_http/$old_https"
        mv "$NGINX_SITE.before-port-change" "$NGINX_SITE"
        env_set WEB_HTTP_PORT "$old_http"; env_set WEB_HTTPS_PORT "$old_https"
        systemctl reload nginx || true
        die "Ports not changed (nginx -t shows the problem, if any)."
    fi
    rm -f "$NGINX_SITE.before-port-change"
    [[ "$http" == "$old_http" ]] || ufw_move "$old_http" "$http"
    [[ "$https" == "$old_https" ]] || ufw_move "$old_https" "$https"
    local address
    address="https://$(env_get SERVER_IP)"; [[ "$https" == 443 ]] || address="$address:$https"
    printf '\n\033[32mDone.\033[0m The address is now %s/\n' "$address"
    echo "Tell the users, and change it in every till program (they still use the old one)."
}

# ----------------------------------------------------------------------------- uninstall
uninstall() {
    local what="services, web site and the backup schedule (data, settings, backups and firewall rules are KEPT)"
    (( REMOVE_DATA )) && what="EVERYTHING: also the database, uploaded files, settings and ALL BACKUPS"
    say "Removing $what"
    confirm "Continue?" no || { note "Cancelled: nothing was changed."; return 0; }
    local unit door http https
    door=$(door_port); http=$(http_port); https=$(https_port)
    for unit in "${BACKEND_SERVICES[@]}" frontend backend-backup.timer backend-basebackup.timer; do
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
    done
    rm -f /etc/systemd/system/backend-{web,ws,tcp,worker}.service /etc/systemd/system/frontend.service \
        /etc/systemd/system/backend-{backup,basebackup}.{service,timer}
    systemctl daemon-reload
    systemctl reset-failed >/dev/null 2>&1 || true   # forget the stopped services' last state
    note "services and backup schedule removed"
    rm -f /etc/nginx/sites-enabled/backend.conf "$NGINX_SITE"
    systemctl reload nginx 2>/dev/null || true
    note "web site removed from nginx"
    if (( REMOVE_DATA )) && ufw_active; then
        local port rule
        for port in "$door" "$http" "$https"; do
            while read -r rule; do [[ -z "$rule" ]] || ufw_delete "$rule"; done <<<"$(ufw_rules_for "$port")"
        done
        note "firewall rules for $http, $https, $door removed (SSH is left open)"
    fi
    rm -f /usr/local/sbin/mgmt
    if (( REMOVE_DATA )); then
        runuser -u postgres -- psql -qc "DROP DATABASE IF EXISTS backend" 2>/dev/null || true
        runuser -u postgres -- psql -qc "DROP ROLE IF EXISTS backend" 2>/dev/null || true
        rm -f /etc/postgresql/*/main/conf.d/90-backend-backup.conf
        systemctl restart postgresql 2>/dev/null || true
        for cmd in node npm npx; do
            [[ "$(readlink -f /usr/local/bin/$cmd 2>/dev/null)" != /opt/node/* ]] || rm -f "/usr/local/bin/$cmd"
        done
        rm -rf "$BASE" "$FRONT" /opt/node "$ETC" /var/lib/backend /var/backups/backend /var/tmp/backend-install /var/tmp/frontend-install
        userdel backend 2>/dev/null || true
        userdel frontend 2>/dev/null || true
        note "database, files, settings and backups removed"
        note "kept: the OS packages (PostgreSQL, Redis, nginx) and developers' backend-dev / frontend-dev"
    else
        note "kept: $BASE, $FRONT, $ETC, /var/lib/backend, /var/backups/backend, the database and the firewall rules"
        note "run the kit's install-all.sh again to bring everything back"
    fi
}

# ----------------------------------------------------------------------------- status
status() {
    say "Services"
    local unit
    for unit in postgresql redis-server nginx "${BACKEND_SERVICES[@]}" frontend backend-backup.timer; do
        printf '    %-24s %s\n' "$unit" "$(systemctl is-active "$unit" 2>/dev/null || true)"
    done
    say "Ports"
    note "web: HTTP $(http_port), HTTPS $(https_port)   doors: TCP $(door_port)   ($ENV_FILE)"
    say "Health"
    local path
    for path in health/ health/db/ health/redis/ health/backup/; do
        printf '    %-24s %s\n' "/$path" "$(http_code "$(https_base)/$path")"
    done
    note "backend release:  $(basename "$(readlink -f "$BASE/current")")"
    note "frontend release: $(basename "$(readlink -f "$FRONT/current" 2>/dev/null || echo none)")"
}

# ----------------------------------------------------------------------------- main
CMD=${1:-}; shift || true
DEV_USER="${SUDO_USER:-}"
ASSUME_YES=0
REMOVE_DATA=0
ARGS=()
while (( $# )); do
    case "$1" in
        --user) DEV_USER="$2"; shift 2 ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --remove-data) REMOVE_DATA=1; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) die "Unknown option: $1 (see: mgmt --help)" ;;
        *) ARGS+=("$1"); shift ;;
    esac
done

case "$CMD" in
    deploy-backend)   need_root; need_installed; deploy_backend; deploy_checks ;;
    deploy-frontend)  need_root; need_installed; deploy_frontend; deploy_checks ;;
    deploy-all)       need_root; need_installed; deploy_backend; deploy_frontend; deploy_checks ;;
    change-door-port) need_root; need_installed; change_door_port ;;
    change-web-port)  need_root; need_installed; change_web_port ;;
    apply-web-ports)  need_root; need_installed; apply_web_ports "$(http_port)" "$(https_port)" ;;   # used by the installer
    uninstall)        need_root; uninstall ;;
    status)           need_installed; status ;;
    ""|-h|--help|help) usage ;;
    *) die "Unknown command: $CMD (see: mgmt --help)" ;;
esac
