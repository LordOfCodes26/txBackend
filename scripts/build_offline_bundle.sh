#!/usr/bin/env bash
# Build a self-contained release bundle for an offline server.
#
# Run on a machine WITH internet that matches the target server's OS, CPU
# architecture and Python version (e.g. Ubuntu 24.04, x86_64, Python 3.12),
# because some wheels (psycopg-binary) are platform-specific.
#
# Output: dist/backend-<version>.tar.gz containing:
#   app/          the application source
#   wheelhouse/   every Python package as a wheel
#   os-packages/  a local apt repository with the OS packages (Python venv, PostgreSQL,
#                 Redis, nginx, ...) and their full dependency tree (skip: --no-os-packages)
# Copy it to the server and run app/scripts/install_offline.sh from inside the extracted folder.
set -euo pipefail

cd "$(dirname "$0")/.."
WITH_OS=1
if [[ "${1:-}" == "--no-os-packages" ]]; then WITH_OS=0; shift; fi
VERSION="${1:-$(date +%Y%m%d-%H%M%S)}"
NAME="backend-${VERSION}"
STAGE="dist/${NAME}"

rm -rf "${STAGE}"
mkdir -p "${STAGE}/app" "${STAGE}/wheelhouse"

echo "==> Building wheels"
python3 -m venv "${STAGE}/.buildenv"
"${STAGE}/.buildenv/bin/pip" install --quiet --upgrade pip wheel
"${STAGE}/.buildenv/bin/pip" wheel --quiet -r requirements/prod.txt -w "${STAGE}/wheelhouse"
rm -rf "${STAGE}/.buildenv"

if (( WITH_OS )); then
    echo "==> Downloading OS packages (with dependencies) into a local apt repository"
    OS_PKGS="python3 python3-venv python3.12-venv postgresql postgresql-16 postgresql-client-16
             postgresql-contrib redis-server nginx openssl rsync ca-certificates tar gzip"
    mkdir -p "${STAGE}/os-packages"
    apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks \
        --no-replaces --no-enhances ${OS_PKGS} 2>/dev/null \
        | grep -E '^[a-zA-Z0-9]' | sort -u > "${STAGE}/os-packages/manifest.txt"
    (cd "${STAGE}/os-packages" && apt-get download -q $(cat manifest.txt) >/dev/null)
    (cd "${STAGE}/os-packages" && apt-ftparchive packages . > Packages && gzip -kf Packages \
        && apt-ftparchive release . > Release)
    echo "    $(ls "${STAGE}"/os-packages/*.deb | wc -l) packages"
fi

echo "==> Copying source (committed files only)"
# git archive includes only tracked files: never .env*, .venv, dist/ or the git history.
if ! git diff --quiet HEAD -- || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
    if [[ "${ALLOW_DIRTY:-}" != "1" ]]; then
        echo "Uncommitted changes: commit them first (or ALLOW_DIRTY=1 to bundle HEAD anyway)." >&2
        exit 1
    fi
fi
git archive --format=tar HEAD | tar -xf - -C "${STAGE}/app"
git rev-parse --short HEAD > "${STAGE}/COMMIT"
for secret in .env .env.staging; do
    [[ ! -e "${STAGE}/app/${secret}" ]] || { echo "Refusing: ${secret} is tracked by git!" >&2; exit 1; }
done
echo "${VERSION}" > "${STAGE}/VERSION"
python3 - > "${STAGE}/BUILT_FOR" <<'PY'
import platform, sys
os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "unknown OS")
print(f"{os_name} {platform.machine()} Python {sys.version.split()[0]}")
PY

echo "==> Packing"
tar -czf "dist/${NAME}.tar.gz" -C dist "${NAME}"
rm -rf "${STAGE}"
(cd dist && sha256sum "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256")
install -m 0755 deploy/install-backend.sh dist/install-backend.sh
echo "Files to copy to the offline server: dist/${NAME}.tar.gz, dist/${NAME}.tar.gz.sha256, dist/install-backend.sh"
echo "Bundle: dist/${NAME}.tar.gz (commit $(git rev-parse --short HEAD), built for: $(tar -xzOf "dist/${NAME}.tar.gz" "${NAME}/BUILT_FOR"))"
