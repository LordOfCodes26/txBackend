#!/usr/bin/env bash
# Build a self-contained release bundle for an offline server.
#
# Run on a machine WITH internet that matches the target server's OS, CPU
# architecture and Python version (e.g. Ubuntu 24.04, x86_64, Python 3.12),
# because some wheels (psycopg-binary) are platform-specific.
#
# Output: dist/backend-<version>.tar.gz containing the app source and every
# Python package as a wheel. Copy it to the server and run
# scripts/install_offline.sh from inside the extracted folder.
set -euo pipefail

cd "$(dirname "$0")/.."
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

echo "==> Copying source"
tar --exclude=.venv --exclude=.env --exclude=dist --exclude=__pycache__ \
    --exclude=.pytest_cache --exclude=.ruff_cache --exclude=media --exclude=staticfiles \
    -cf - . | tar -xf - -C "${STAGE}/app"
echo "${VERSION}" > "${STAGE}/VERSION"
python3 - > "${STAGE}/BUILT_FOR" <<'PY'
import platform, sys
os_name = platform.freedesktop_os_release().get("PRETTY_NAME", "unknown OS")
print(f"{os_name} {platform.machine()} Python {sys.version.split()[0]}")
PY

echo "==> Packing"
tar -czf "dist/${NAME}.tar.gz" -C dist "${NAME}"
rm -rf "${STAGE}"
echo "Bundle: dist/${NAME}.tar.gz (built for: $(tar -xzOf "dist/${NAME}.tar.gz" "${NAME}/BUILT_FOR"))"
