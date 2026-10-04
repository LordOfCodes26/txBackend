#!/usr/bin/env bash
# Build the WINDOWS offline kit: backend + frontend + every program they need, for an
# offline Windows 10/11 PC. Runs on Linux with internet (all Windows files are downloaded,
# Python packages as Windows wheels).
#
#   scripts/build_windows_kit.sh VERSION [FRONTEND_PACKAGE]
#
# FRONTEND_PACKAGE is made in the frontend project with scripts/package-offline-windows.sh
# (default: ../frontend/dist/management-app-offline-windows.tar.gz).
#
# Output: dist/windows-kit-VERSION.zip (and the folder dist/windows-kit-VERSION/) with
#   install.cmd, install-all.ps1, setup-dev.ps1, uninstall.ps1, ...  the installer
#   README.md                                   the step-by-step guide
#   backend-VERSION-windows.tar.gz              code, Windows wheels, git history
#   management-app-offline-windows.tar.gz       frontend source + Windows node_modules
#   runtime/                                    Python, Node.js, PostgreSQL, .NET, Garnet,
#                                               Caddy, WinSW, Git (versions.txt)
#   SHA256SUMS                                  checked by the installer
set -euo pipefail

cd "$(dirname "$0")/.."
VERSION="${1:?usage: scripts/build_windows_kit.sh VERSION [FRONTEND_PACKAGE]}"
FRONTEND_PKG="${2:-$(cd .. && pwd)/frontend/dist/management-app-offline-windows.tar.gz}"

# Pinned versions of the Windows programs. Python must stay 3.12 (the wheels are cp312);
# PostgreSQL's major version must match existing installations (16).
PYTHON_VERSION=3.12.10
PG_VERSION=16.13
DOTNET_VERSION=8.0.31
GARNET_VERSION=2.2.0
CADDY_VERSION=2.11.6
WINSW_VERSION=2.12.0
GIT_VERSION=2.56.0

NAME="windows-kit-${VERSION}"
KIT="dist/${NAME}"
CACHE="dist/windows-cache"   # downloads are kept here and reused by the next build
BNAME="backend-${VERSION}-windows"
STAGE="dist/${BNAME}"

say() { printf '\n==> %s\n' "$*"; }
zip_has() {  # zip_has ZIP REGEX: does the archive hold a matching path? (no unzip needed)
    python3 -c 'import re, sys, zipfile
sys.exit(0 if any(re.search(sys.argv[2], n) for n in zipfile.ZipFile(sys.argv[1]).namelist()) else 1)' "$1" "$2"
}
die() { echo "ERROR: $*" >&2; exit 1; }
fetch() {  # fetch URL FILE: download into the cache once
    [[ -s "$CACHE/$2" ]] && return
    curl -fsSL --retry 3 -o "$CACHE/$2.partial" "$1"
    mv "$CACHE/$2.partial" "$CACHE/$2"
}

[[ -f "$FRONTEND_PKG" ]] || die "Frontend package not found: $FRONTEND_PKG (make it with the frontend's scripts/package-offline-windows.sh)"
if ! git diff --quiet HEAD -- || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
    [[ "${ALLOW_DIRTY:-}" == "1" ]] || die "Uncommitted changes: commit them first (or ALLOW_DIRTY=1 to build HEAD anyway)."
fi

say "Checking the frontend package"
listing=$(tar -tzf "$FRONTEND_PKG")
grep -qE '^(\./)?management-app/package\.json$' <<<"$listing" || die "No management-app/package.json in $FRONTEND_PKG"
grep -qE '^(\./)?management-app/node_modules/@next/swc-win32-x64-msvc/' <<<"$listing" || die "No Windows node_modules in $FRONTEND_PKG"
NODE_ZIP=$(grep -oE '^(\./)?node-v[0-9.]+-win-x64\.zip$' <<<"$listing" | head -1) || die "No Node.js for Windows in $FRONTEND_PKG"
NODE_VERSION=$(sed -E 's/^(\.\/)?node-v([0-9.]+)-win-x64\.zip$/\2/' <<<"$NODE_ZIP")
echo "    ok: source, Windows node_modules, Node.js $NODE_VERSION"

rm -rf "$KIT" "$STAGE" "dist/${NAME}.zip"
mkdir -p "$CACHE" "$KIT/runtime" "$STAGE/app" "$STAGE/wheelhouse"

# ------------------------------------------------------------------------------- programs
say "Windows programs (cached in $CACHE)"
PY_FILE="python-${PYTHON_VERSION}.nupkg"
fetch "https://api.nuget.org/v3-flatcontainer/python/${PYTHON_VERSION}/python.${PYTHON_VERSION}.nupkg" "$PY_FILE"
zip_has "$CACHE/$PY_FILE" '^tools/python\.exe$' || die "$PY_FILE has no tools/python.exe"

PG_RAW="postgresql-${PG_VERSION}-1-windows-x64-binaries.zip"
fetch "https://get.enterprisedb.com/postgresql/${PG_RAW}" "$PG_RAW"

DOTNET_FILE="dotnet-runtime-${DOTNET_VERSION}-win-x64.zip"
fetch "https://builds.dotnet.microsoft.com/dotnet/Runtime/${DOTNET_VERSION}/${DOTNET_FILE}" "$DOTNET_FILE"
dotnet_hash=$(curl -fsSL https://dotnetcli.blob.core.windows.net/dotnet/release-metadata/8.0/releases.json \
    | python3 -c "
import json, sys
for r in json.load(sys.stdin)['releases']:
    for f in (r.get('runtime') or {}).get('files', []):
        if f['name'] == 'dotnet-runtime-win-x64.zip' and '/${DOTNET_VERSION}/' in f['url']:
            print(f['hash']); sys.exit()")
[[ "$(sha512sum "$CACHE/$DOTNET_FILE" | cut -d' ' -f1)" == "$dotnet_hash" ]] || die ".NET runtime doesn't match its published checksum"

GARNET_RAW="garnet-${GARNET_VERSION}-win-x64-based-readytorun.zip"
fetch "https://github.com/microsoft/garnet/releases/download/v${GARNET_VERSION}/win-x64-based-readytorun.zip" "$GARNET_RAW"

CADDY_FILE="caddy_${CADDY_VERSION}_windows_amd64.zip"
fetch "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/${CADDY_FILE}" "$CADDY_FILE"
curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_checksums.txt" \
    | grep " ${CADDY_FILE}\$" | (cd "$CACHE" && sha512sum --check --quiet -) || die "Caddy doesn't match its published checksum"

WINSW_FILE="WinSW-x64-${WINSW_VERSION}.exe"
fetch "https://github.com/winsw/winsw/releases/download/v${WINSW_VERSION}/WinSW-x64.exe" "$WINSW_FILE"

GIT_FILE="Git-${GIT_VERSION}-64-bit.exe"
fetch "https://github.com/git-for-windows/git/releases/download/v${GIT_VERSION}.windows.1/${GIT_FILE}" "$GIT_FILE"
git_hash=$(curl -fsSL "https://api.github.com/repos/git-for-windows/git/releases/tags/v${GIT_VERSION}.windows.1" \
    | python3 -c "
import json, re, sys
body = json.load(sys.stdin).get('body', '')
m = re.search(r'${GIT_FILE//./\\.}\s*\|\s*([0-9a-f]{64})', body)
print(m.group(1) if m else '')")
if [[ -n "$git_hash" ]]; then
    [[ "$(sha256sum "$CACHE/$GIT_FILE" | cut -d' ' -f1)" == "$git_hash" ]] || die "Git for Windows doesn't match its published checksum"
else
    echo "    WARNING: no published checksum found for $GIT_FILE"
fi

# Trim what the server doesn't need: pgAdmin/StackBuilder/docs from PostgreSQL (~70% of
# its size), and Garnet's .NET 10 build (the kit carries the .NET 8 runtime).
PG_FILE="postgresql-${PG_VERSION}-windows-x64.zip"
GARNET_FILE="garnet-${GARNET_VERSION}-win-x64.zip"
python3 - "$CACHE" "$PG_RAW" "$KIT/runtime/$PG_FILE" "$GARNET_RAW" "$KIT/runtime/$GARNET_FILE" <<'PY'
import shutil, sys, zipfile
cache, pg_in, pg_out, garnet_in, garnet_out = sys.argv[1:]

def repack(src, dst, rename):
    kept = 0
    with zipfile.ZipFile(f"{cache}/{src}") as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zout:
        for info in zin.infolist():
            name = rename(info.filename)
            if not name or info.is_dir():
                continue
            out = zipfile.ZipInfo(name, info.date_time)
            out.compress_type = zipfile.ZIP_DEFLATED
            with zin.open(info) as fin, zout.open(out, "w") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
            kept += 1
    return kept

drop = ("pgsql/pgAdmin 4/", "pgsql/StackBuilder/", "pgsql/doc/", "pgsql/symbols/")
n = repack(pg_in, pg_out, lambda f: None if f.startswith(drop) else f)
print(f"    PostgreSQL: {n} files")
n = repack(garnet_in, garnet_out, lambda f: f[len("net8.0/"):] if f.startswith("net8.0/") else None)
print(f"    Garnet (net8.0): {n} files")
PY
zip_has "$KIT/runtime/$PG_FILE" '^pgsql/bin/pg_ctl\.exe$' || die "PostgreSQL zip has no pgsql/bin/pg_ctl.exe"
zip_has "$KIT/runtime/$PG_FILE" 'btree_gist\.control$' || die "PostgreSQL zip has no btree_gist extension"
zip_has "$KIT/runtime/$GARNET_FILE" '^GarnetServer\.exe$' || die "Garnet zip has no GarnetServer.exe"
zip_has "$CACHE/$CADDY_FILE" '^caddy\.exe$' || die "Caddy zip has no caddy.exe"
cp "$CACHE/$PY_FILE" "$CACHE/$DOTNET_FILE" "$CACHE/$CADDY_FILE" "$CACHE/$GIT_FILE" "$KIT/runtime/"
cp "$CACHE/$WINSW_FILE" "$KIT/runtime/$WINSW_FILE"
tar -xzf "$FRONTEND_PKG" -C "$KIT/runtime" "$NODE_ZIP"
NODE_FILE=$(basename "$NODE_ZIP")
cat > "$KIT/runtime/versions.txt" <<EOF
PYTHON_FILE=$PY_FILE
PYTHON_VERSION=$PYTHON_VERSION
NODE_FILE=$NODE_FILE
NODE_VERSION=$NODE_VERSION
PG_FILE=$PG_FILE
PG_VERSION=$PG_VERSION
DOTNET_FILE=$DOTNET_FILE
DOTNET_VERSION=$DOTNET_VERSION
GARNET_FILE=$GARNET_FILE
GARNET_VERSION=$GARNET_VERSION
CADDY_FILE=$CADDY_FILE
CADDY_VERSION=$CADDY_VERSION
WINSW_FILE=$WINSW_FILE
WINSW_VERSION=$WINSW_VERSION
GIT_FILE=$GIT_FILE
GIT_VERSION=$GIT_VERSION
EOF
(cd "$KIT/runtime" && ls -la | awk 'NR>1 && $NF != "." && $NF != ".." {printf "    %-48s %6.1f MB\n", $NF, $5/1048576}')

# ------------------------------------------------------------------------------- backend
say "Backend bundle: code, history and Windows wheels"
git archive --format=tar HEAD | tar -xf - -C "$STAGE/app"
git rev-parse --short HEAD > "$STAGE/COMMIT"
git bundle create "$STAGE/backend.git-bundle" --branches --tags 2>/dev/null
for secret in .env .env.staging; do
    [[ ! -e "$STAGE/app/$secret" ]] || die "Refusing: $secret is tracked by git!"
done
echo "$VERSION" > "$STAGE/VERSION"
echo "Windows 10/11 x64, Python ${PYTHON_VERSION}" > "$STAGE/BUILT_FOR"
python3 -m venv "$STAGE/.buildenv"
"$STAGE/.buildenv/bin/pip" install --quiet --upgrade pip uv
# Resolve the package list FOR WINDOWS first: `pip download --platform` still evaluates
# markers such as `sys_platform == "win32"` for this Linux machine, so Windows-only
# dependencies (colorama, needed by pytest) would be left out. uv resolves for the target.
"$STAGE/.buildenv/bin/uv" pip compile --quiet --no-header \
    --python-platform x86_64-pc-windows-msvc --python-version 3.12 \
    -c requirements/constraints.txt requirements/windows.txt requirements/dev.txt \
    -o "$STAGE/windows-requirements.txt"
"$STAGE/.buildenv/bin/pip" download --quiet --no-deps --dest "$STAGE/wheelhouse" \
    --platform win_amd64 --python-version 3.12 --implementation cp --only-binary=:all: \
    -r "$STAGE/windows-requirements.txt"
# Every resolved package must have its wheel (what the offline installs will ask for).
python3 - "$STAGE/windows-requirements.txt" "$STAGE/wheelhouse" <<'PY'
import os, re, sys
norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()
wanted = {norm(l.split("==")[0]) for l in open(sys.argv[1]) if "==" in l and not l.lstrip().startswith("#")}
have = {norm(f.split("-")[0]) for f in os.listdir(sys.argv[2]) if f.endswith(".whl")}
missing = sorted(wanted - have)
sys.exit(f"Windows wheels missing: {missing}" if missing else 0)
PY
rm -rf "$STAGE/.buildenv" "$STAGE/windows-requirements.txt"
echo "    $(ls "$STAGE/wheelhouse" | wc -l) Windows wheels (running and development)"
tar -czf "$KIT/${BNAME}.tar.gz" -C dist "$BNAME"
rm -rf "$STAGE"

# ------------------------------------------------------------------------------- kit
say "Assembling $KIT"
cp "$FRONTEND_PKG" "$KIT/management-app-offline-windows.tar.gz"
# The installer scripts and guide, from the committed code (same commit as the bundle).
mkdir -p "$KIT/.scripts"
git archive --format=tar HEAD deploy/windows | tar -xf - -C "$KIT/.scripts"
for f in install.cmd uninstall.bat install-all.ps1 common.ps1 postgres.ps1 setup-dev.ps1 uninstall.ps1 backup.ps1 restore.ps1; do
    cp "$KIT/.scripts/deploy/windows/$f" "$KIT/$f"
done
cp "$KIT/.scripts/deploy/windows/README.md" "$KIT/README.md"
rm -rf "$KIT/.scripts"
for f in "$KIT/install.cmd" "$KIT/uninstall.bat"; do   # cmd.exe wants CRLF
    grep -q $'\r$' "$f" || sed -i 's/$/\r/' "$f"
done
(cd "$KIT" && find . -type f ! -name SHA256SUMS | sed 's#^\./##' | sort | xargs sha256sum > SHA256SUMS)

say "Packing dist/${NAME}.zip"
python3 - "$KIT" "dist/${NAME}.zip" <<'PY'
import os, sys, zipfile
kit, out = sys.argv[1:]
base = os.path.dirname(kit)
stored = (".gz", ".zip", ".nupkg", ".exe")   # already compressed
with zipfile.ZipFile(out, "w", allowZip64=True) as z:
    for root, _, files in os.walk(kit):
        for f in sorted(files):
            path = os.path.join(root, f)
            mode = zipfile.ZIP_STORED if f.endswith(stored) else zipfile.ZIP_DEFLATED
            z.write(path, os.path.relpath(path, base), compress_type=mode)
PY
(cd dist && sha256sum "${NAME}.zip" > "${NAME}.zip.sha256")
echo
du -sh "$KIT" "dist/${NAME}.zip"
ls -la "$KIT"
echo "Copy dist/${NAME}.zip to the Windows PC, unpack it and double-click install.cmd (see README.md)."
