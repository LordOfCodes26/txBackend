#!/usr/bin/env bash
# Prepare a freshly installed Ubuntu Server 24.04 for the backend, BEFORE install-backend.sh.
# Works without internet.
#
#   sudo bash prepare-server.sh             # asks step by step (Enter keeps the current value)
#   sudo bash prepare-server.sh --dry-run   # only shows what it would do
#
# Steps: check the system, server name, timezone and clock, fixed IP address (netplan),
# firewall (ufw), switch off Ubuntu's online auto-updates. At the end it prints the
# install-backend.sh command to run next.
#
# Options (skip the question for that step):
#   --hostname NAME          server name, e.g. backend-server
#   --timezone Area/City     e.g. Asia/Pyongyang
#   --ntp SERVER             company time server (IP or name); "none" = set the clock by hand
#   --interface NAME         network card, e.g. eth0 or ens18 (see: ip -br link)
#   --ip ADDRESS/PREFIX      fixed IP with prefix, e.g. 192.168.1.10/24
#   --gateway IP             default gateway ("none" for a network without one)
#   --dns "IP,IP"            DNS servers ("none" for no DNS)
#   --door-network CIDR      only these addresses may use the door port 9100, e.g. 192.168.1.0/24
#   --skip-network           keep the network as it is
#   --skip-firewall          don't touch the firewall
#   --yes                    don't ask: use the options and current values
#   --dry-run                show what would be done, change nothing
set -euo pipefail

HOSTNAME_NEW="" TIMEZONE="" NTP="" IFACE="" IP_CIDR="" GATEWAY="" DNS="" DOOR_NET=""
SKIP_NETWORK=0 SKIP_FIREWALL=0 ASSUME_YES=0 DRY_RUN=0

while (( $# )); do
    case "$1" in
        --hostname) HOSTNAME_NEW="$2"; shift 2 ;;
        --timezone) TIMEZONE="$2"; shift 2 ;;
        --ntp) NTP="$2"; shift 2 ;;
        --interface) IFACE="$2"; shift 2 ;;
        --ip) IP_CIDR="$2"; shift 2 ;;
        --gateway) GATEWAY="$2"; shift 2 ;;
        --dns) DNS="$2"; shift 2 ;;
        --door-network) DOOR_NET="$2"; shift 2 ;;
        --skip-network) SKIP_NETWORK=1; shift ;;
        --skip-firewall) SKIP_FIREWALL=1; shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { echo "    $*"; }
warn() { printf '    \033[33mWARNING: %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }
ask() {  # ask "question" default -> answer (Enter keeps the default)
    local answer
    if (( ASSUME_YES )); then echo "$2"; return; fi
    read -r -p "    $1 [$2]: " answer </dev/tty || true
    echo "${answer:-$2}"
}
run() {  # run a command, or only show it with --dry-run
    if (( DRY_RUN )); then echo "    [dry-run] $*"; else "$@"; fi
}
write_file() {  # write_file PATH MODE  (content on stdin)
    local path="$1" mode="$2" content
    content=$(cat)
    if (( DRY_RUN )); then
        echo "    [dry-run] would write $path:"
        sed 's/^/        | /' <<<"$content"
    else
        mkdir -p "$(dirname "$path")"
        printf '%s\n' "$content" > "$path"
        chmod "$mode" "$path"
    fi
}
valid_ipv4() { [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] \
    && (( BASH_REMATCH[1] <= 255 && BASH_REMATCH[2] <= 255 && BASH_REMATCH[3] <= 255 && BASH_REMATCH[4] <= 255 )); }
valid_cidr() { [[ "$1" == */* ]] && valid_ipv4 "${1%/*}" && [[ "${1#*/}" =~ ^[0-9]+$ ]] && (( ${1#*/} >= 8 && ${1#*/} <= 32 )); }

(( DRY_RUN )) && echo "Dry run: nothing will be changed."
(( EUID == 0 || DRY_RUN )) || die "Run as root: sudo bash $0"

# ---------------------------------------------------------------------------------- 1. check
say "1. Checking the system"
read -r OS_ID OS_VERSION_ID < <(. /etc/os-release && echo "$ID $VERSION_ID")
note "system: $(. /etc/os-release && echo "$PRETTY_NAME") $(uname -m)"
[[ "$OS_ID" == ubuntu && "$OS_VERSION_ID" == 24.04 && "$(uname -m)" == x86_64 ]] \
    || die "The backend bundle needs Ubuntu 24.04 on x86_64."
FREE_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
MEM_GB=$(awk '/MemTotal/ {printf "%d", $2 / 1048576 + 0.5}' /proc/meminfo)
note "free disk: ${FREE_GB} GB, memory: ${MEM_GB} GB"
(( FREE_GB >= 10 )) || warn "less than 10 GB free on /: the database and its backups need room."
(( MEM_GB >= 2 )) || warn "less than 2 GB of memory: the backend may be slow."
for cmd in netplan timedatectl ip; do
    command -v "$cmd" >/dev/null || die "Missing '$cmd': is this Ubuntu Server?"
done

# ---------------------------------------------------------------------------------- 2. name
say "2. Server name"
CURRENT_HOSTNAME=$(hostname)
[[ -n "$HOSTNAME_NEW" ]] || HOSTNAME_NEW=$(ask "Server name" "$CURRENT_HOSTNAME")
[[ "$HOSTNAME_NEW" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]] \
    || die "Not a valid server name: $HOSTNAME_NEW (letters, digits and -)"
if [[ "$HOSTNAME_NEW" != "$CURRENT_HOSTNAME" ]]; then
    run hostnamectl set-hostname "$HOSTNAME_NEW"
    if ! grep -qw "$HOSTNAME_NEW" /etc/hosts; then
        if (( DRY_RUN )); then echo "    [dry-run] add '127.0.1.1 $HOSTNAME_NEW' to /etc/hosts"
        else echo "127.0.1.1 $HOSTNAME_NEW" >> /etc/hosts; fi
    fi
    note "server name: $HOSTNAME_NEW"
else
    note "kept: $CURRENT_HOSTNAME"
fi

# ---------------------------------------------------------------------------------- 3. time
say "3. Timezone and clock"
CURRENT_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)
[[ -n "$TIMEZONE" ]] || TIMEZONE=$(ask "Timezone (e.g. Asia/Pyongyang, Asia/Seoul)" "$CURRENT_TZ")
[[ -f "/usr/share/zoneinfo/$TIMEZONE" ]] || die "Unknown timezone: $TIMEZONE (list: timedatectl list-timezones)"
[[ "$TIMEZONE" == "$CURRENT_TZ" ]] || run timedatectl set-timezone "$TIMEZONE"
note "timezone: $TIMEZONE"

# Without internet the clock needs a company time server, or is set by hand. Attendance and
# purchases are recorded with this clock, so it must be right.
[[ -n "$NTP" ]] || NTP=$(ask "Company time server (IP or name), or 'none' to set the clock by hand" "none")
if [[ "$NTP" != none ]]; then
    write_file /etc/systemd/timesyncd.conf.d/backend.conf 644 <<EOF
# Written by prepare-server.sh: the company's time server (no internet here).
[Time]
NTP=$NTP
EOF
    run timedatectl set-ntp true
    run systemctl restart systemd-timesyncd
    note "time from: $NTP (check later with: timedatectl timesync-status)"
else
    run timedatectl set-ntp false
    note "the clock now says: $(date '+%Y-%m-%d %H:%M:%S')"
    if (( ! ASSUME_YES )); then
        answer=$(ask "Is that right? Enter 'yes', or the correct time as YYYY-MM-DD HH:MM:SS" "yes")
        if [[ "$answer" != yes ]]; then
            [[ "$answer" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}$ ]] \
                || die "Write the time as YYYY-MM-DD HH:MM:SS"
            run timedatectl set-time "$answer"
            note "clock set to $answer"
        fi
    fi
fi

# ---------------------------------------------------------------------------------- 4. network
say "4. Fixed IP address"
SERVER_IP=""
if (( SKIP_NETWORK )); then
    note "skipped (--skip-network)"
else
    DEFAULT_IFACE=$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')
    [[ -n "$DEFAULT_IFACE" ]] || DEFAULT_IFACE=$(ip -o link show | awk -F': ' '$2 != "lo" {print $2; exit}')
    note "network cards: $(ip -br link | awk '$1 != "lo" {printf "%s(%s) ", $1, $2}')"
    [[ -n "$IFACE" ]] || IFACE=$(ask "Network card for the company network" "$DEFAULT_IFACE")
    ip link show "$IFACE" >/dev/null 2>&1 || die "No network card named $IFACE (see: ip -br link)"

    CURRENT_CIDR=$(ip -o -4 addr show dev "$IFACE" | awk '{print $4; exit}')
    CURRENT_GW=$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
    CURRENT_DNS=$(resolvectl dns "$IFACE" 2>/dev/null | cut -d: -f2- | xargs | tr ' ' ',' || true)
    note "now: ${CURRENT_CIDR:-no IPv4 address}, gateway ${CURRENT_GW:-none}, DNS ${CURRENT_DNS:-none}"

    [[ -n "$IP_CIDR" ]] || IP_CIDR=$(ask "Fixed IP with prefix (e.g. 192.168.1.10/24)" "${CURRENT_CIDR:-192.168.1.10/24}")
    valid_cidr "$IP_CIDR" || die "Write the IP with its prefix, e.g. 192.168.1.10/24 (got: $IP_CIDR)"
    [[ -n "$GATEWAY" ]] || GATEWAY=$(ask "Gateway, or 'none'" "${CURRENT_GW:-none}")
    [[ "$GATEWAY" == none ]] || valid_ipv4 "$GATEWAY" || die "Not an IP address: $GATEWAY"
    [[ -n "$DNS" ]] || DNS=$(ask "DNS servers separated by commas, or 'none'" "${CURRENT_DNS:-none}")
    if [[ "$DNS" != none ]]; then
        for server in ${DNS//,/ }; do valid_ipv4 "$server" || die "Not an IP address: $server"; done
    fi
    SERVER_IP="${IP_CIDR%/*}"

    YAML="network:
  version: 2
  ethernets:
    $IFACE:
      dhcp4: false
      addresses: [$IP_CIDR]"
    [[ "$GATEWAY" == none ]] || YAML+="
      routes:
        - to: default
          via: $GATEWAY"
    [[ "$DNS" == none ]] || YAML+="
      nameservers:
        addresses: [${DNS//,/, }]"

    if [[ "$IP_CIDR" == "$CURRENT_CIDR" && "$GATEWAY" == "${CURRENT_GW:-none}" ]] \
        && grep -qs "dhcp4: false" /etc/netplan/99-backend-static.yaml; then
        note "already fixed at $IP_CIDR: nothing to change"
    else
        # cloud-init would rewrite the network on the next boot: stop it doing that.
        if [[ -d /etc/cloud ]]; then
            write_file /etc/cloud/cloud.cfg.d/99-disable-network-config.cfg 644 <<<"network: {config: disabled}"
        fi
        write_file /etc/netplan/99-backend-static.yaml 600 <<<"# Written by prepare-server.sh: fixed address for the backend server.
$YAML"
        if (( DRY_RUN )); then
            echo "    [dry-run] netplan generate && netplan apply (or netplan try over SSH)"
        else
            netplan generate || die "netplan rejected the settings (see above); /etc/netplan/99-backend-static.yaml"
            if [[ -n "${SSH_CONNECTION:-}" && $ASSUME_YES -eq 0 ]]; then
                warn "You are connected over SSH. If the new address is different, reconnect to $SERVER_IP."
                note "netplan try: press Enter within 2 minutes to KEEP the new settings;"
                note "otherwise they are undone automatically (so you can't lock yourself out)."
                netplan try --timeout 120 </dev/tty || die "Network change undone. Nothing else changed."
            else
                netplan apply
            fi
        fi
        note "fixed IP: $IP_CIDR on $IFACE"
    fi
fi
[[ -n "$SERVER_IP" ]] || SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

# ---------------------------------------------------------------------------------- 5. firewall
say "5. Firewall"
if (( SKIP_FIREWALL )); then
    note "skipped (--skip-firewall)"
elif ! command -v ufw >/dev/null; then
    warn "ufw is not installed: no firewall set (allow 22, 80, 443 and 9100 on your network firewall)."
else
    [[ -n "$DOOR_NET" ]] || DOOR_NET=$(ask "Network of the door devices for port 9100 (e.g. 192.168.1.0/24), or 'any'" "any")
    [[ "$DOOR_NET" == any ]] || valid_cidr "$DOOR_NET" || die "Write the door network like 192.168.1.0/24"
    run ufw allow 22/tcp comment 'SSH'
    run ufw allow 80/tcp comment 'backend web (redirect)'
    run ufw allow 443/tcp comment 'backend web/API'
    if [[ "$DOOR_NET" == any ]]; then
        run ufw allow 9100/tcp comment 'backend doors'
    else
        run ufw allow from "$DOOR_NET" to any port 9100 proto tcp comment 'backend doors'
    fi
    run ufw --force enable
    note "open: 22 (SSH), 80, 443 (web), 9100 (doors, from ${DOOR_NET})"
fi

# ---------------------------------------------------------------------------------- 6. updates
say "6. Offline: switch off automatic online updates"
for unit in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
        run systemctl disable --now "$unit" 2>/dev/null || true
    fi
done
note "done (they only fail without internet; updates come with new bundles)"

# ---------------------------------------------------------------------------------- summary
say "The server is ready for the backend"
cat <<EOF
    name:      ${HOSTNAME_NEW}
    timezone:  ${TIMEZONE}   (clock: $(date '+%Y-%m-%d %H:%M'))
    IP:        ${SERVER_IP:-unknown}

    Next, in the folder with the backend files:

    sudo bash install-backend.sh --server-ip ${SERVER_IP:-<server-ip>} --timezone ${TIMEZONE}
EOF
