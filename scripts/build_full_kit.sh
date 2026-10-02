#!/usr/bin/env bash
# Build the FULL offline kit: backend + frontend, running and development, in one folder.
# Run on a machine with internet, Ubuntu 24.04 x86_64 (same as the offline server).
#
#   scripts/build_full_kit.sh VERSION [FRONTEND_PACKAGE]
#
# FRONTEND_PACKAGE is the frontend's offline package, made in the frontend project with
# scripts/package-offline.sh (default: /root/frontend/dist/management-app-offline.tar.gz):
# the repository with .git and node_modules, plus the Node.js runtime.
#
# Output: dist/offline-kit-VERSION/ with everything to copy to the offline server:
#   backend-VERSION.tar.gz (+ .sha256)        backend bundle (see build_offline_bundle.sh)
#   management-app-offline.tar.gz (+ .sha256) frontend package
#   install-all.sh       one installer for everything      prepare-server.sh  fresh Ubuntu prep
#   install-backend.sh   backend only (used by install-all) setup-dev.sh       backend dev copy
#   README.md            the step-by-step guide
set -euo pipefail

cd "$(dirname "$0")/.."
VERSION="${1:?usage: scripts/build_full_kit.sh VERSION [FRONTEND_PACKAGE]}"
FRONTEND_PKG="${2:-/root/frontend/dist/management-app-offline.tar.gz}"
KIT="dist/offline-kit-${VERSION}"

[[ -f "$FRONTEND_PKG" ]] || {
    echo "Frontend package not found: $FRONTEND_PKG" >&2
    echo "Make it in the frontend project first: scripts/package-offline.sh" >&2
    exit 1
}
echo "==> Checking the frontend package"
listing=$(tar -tzf "$FRONTEND_PKG")
grep -qE '^(\./)?management-app/package\.json$' <<<"$listing" || { echo "No management-app/package.json in $FRONTEND_PKG" >&2; exit 1; }
grep -qE '^(\./)?management-app/node_modules/' <<<"$listing" || { echo "No node_modules in $FRONTEND_PKG" >&2; exit 1; }
grep -qE '^(\./)?node-v[0-9.]+-linux-x64\.tar\.xz$' <<<"$listing" || { echo "No Node.js runtime (node-v*-linux-x64.tar.xz) in $FRONTEND_PKG" >&2; exit 1; }
# The offline server can't download fonts: next/font/google makes the build fail there.
if tar -xzOf "$FRONTEND_PKG" --wildcards '*management-app/src/*' 2>/dev/null \
        | grep -qE 'next/font/google|fonts\.(googleapis|gstatic)\.com'; then
    echo "The frontend still loads Google Fonts (next/font/google): it can't build offline." >&2
    echo "Use next/font/local with the font files in the repository, then package again." >&2
    exit 1
fi
if grep -qE '^(\./)?management-app/\.env\.local$' <<<"$listing"; then
    echo "WARNING: the frontend package contains .env.local (machine-specific settings, maybe secrets)." >&2
fi
echo "    ok: source, node_modules, $(grep -oE 'node-v[0-9.]+-linux-x64' <<<"$listing" | head -1)"

echo "==> Building the backend bundle"
rm -rf "$KIT"
scripts/build_offline_bundle.sh "$VERSION" | tail -1

echo "==> Assembling $KIT"
mkdir -p "$KIT"
mv "dist/backend-${VERSION}.tar.gz" "dist/backend-${VERSION}.tar.gz.sha256" "$KIT/"
cp "$FRONTEND_PKG" "$KIT/management-app-offline.tar.gz"
(cd "$KIT" && sha256sum management-app-offline.tar.gz > management-app-offline.tar.gz.sha256)
install -m 0755 deploy/install-all.sh deploy/install-backend.sh deploy/prepare-server.sh \
    deploy/setup-dev.sh "$KIT/"
install -m 0644 deploy/README-OFFLINE-KIT.md "$KIT/README.md"
rm -f dist/install-backend.sh dist/prepare-server.sh dist/setup-dev.sh dist/README-INSTALL.md
echo
du -sh "$KIT"
ls -la "$KIT"
echo "Copy the whole folder $KIT to the offline server and follow its README.md."
