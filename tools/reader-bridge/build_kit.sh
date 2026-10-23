#!/usr/bin/env bash
# Build the offline Windows kit for the reader bridge on Linux (no Windows needed), with
# Wine and the official Windows Python. Needs internet and:
# apt-get install --no-install-recommends wine64 xvfb
#
#   tools/reader-bridge/build_kit.sh VERSION [OUTPUT_DIR]     (default: dist/)
#   SERVER_IP=192.168.1.10 tools/reader-bridge/build_kit.sh VERSION
#
# SERVER_IP (optional) goes into the kit's server.txt: install.bat then offers it, and the
# person installing only presses Enter.
#
# Output: reader-bridge-kit-VERSION.zip (+ .sha256) holding ReaderBridge.exe (Python and
# pyserial built in: the PC needs no Python), nssm.exe (runs it as a service),
# install.bat, uninstall.bat, README.md, driver/ (WCH CH341 USB driver for Windows 10/11:
# the readers' chip; an offline PC can't get it from Windows Update) and server.txt (with
# SERVER_IP).
set -euo pipefail
exec </dev/null

VERSION="${1:?usage: tools/reader-bridge/build_kit.sh VERSION [OUTPUT_DIR]}"
PY_VERSION=3.12.10
NSSM_VERSION=2.24
CH341_URL="https://file.wch.cn/download/file?id=5"  # CH341SER.ZIP from wch-ic.com
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(realpath -m "${2:-$HERE/dist}")"
WORK="$(mktemp -d)"
trap '/usr/lib/wine/wineserver64 -k 2>/dev/null || true; rm -rf "$WORK"' EXIT
export WINEPREFIX="$WORK/wine" WINEDEBUG=-all
WINE=/usr/lib/wine/wine64
[[ -x "$WINE" ]] || { echo "Install wine64 first (apt-get install --no-install-recommends wine64)" >&2; exit 1; }

echo "==> Windows Python $PY_VERSION"
curl -fsSL -o "$WORK/python.nupkg" "https://api.nuget.org/v3-flatcontainer/python/$PY_VERSION/python.$PY_VERSION.nupkg"
xvfb-run -a "$WINE" wineboot -i </dev/null >/dev/null 2>&1 || true
# Windows Python under Wine can't write straight to a file (no terminal, e.g. cron/CI):
# its output always goes through a pipe.
wpy() { "$WINE" 'C:\Py\python.exe' "$@" </dev/null 2>&1 | cat; }
PY="$WINEPREFIX/drive_c/Py"
python3 - "$WORK/python.nupkg" "$PY" <<'PYEOF'
import os, sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
for name in z.namelist():
    if name.startswith("tools/") and not name.endswith("/"):
        target = sys.argv[2] + "/" + name[len("tools/"):]
        os.makedirs(os.path.dirname(target), exist_ok=True)
        open(target, "wb").write(z.read(name))
PYEOF

echo "==> PyInstaller"
wpy -m pip install -q --no-warn-script-location pyinstaller pyserial
mkdir -p "$WINEPREFIX/drive_c/build"
cp "$HERE/reader_bridge.py" "$WINEPREFIX/drive_c/build/"
# Console program: the service's output goes to the log file.
wpy -m PyInstaller --noconfirm --onefile --console --name ReaderBridge \
    --distpath 'C:\build\dist' --workpath 'C:\build\work' --specpath 'C:\build' \
    'C:\build\reader_bridge.py' | tail -2
EXE="$WINEPREFIX/drive_c/build/dist/ReaderBridge.exe"
[[ -f "$EXE" ]] || { echo "PyInstaller did not build ReaderBridge.exe" >&2; exit 1; }
"$WINE" "$EXE" --help </dev/null 2>&1 | cat | grep -q -- "--list-ports" \
    || { echo "ReaderBridge.exe does not run" >&2; exit 1; }

echo "==> NSSM $NSSM_VERSION"
curl -fsSL -o "$WORK/nssm.zip" "https://nssm.cc/release/nssm-$NSSM_VERSION.zip"

echo "==> CH341 driver"
curl -fsSL -o "$WORK/ch341.zip" "$CH341_URL"

echo "==> Kit"
KIT="$WORK/reader-bridge-kit-$VERSION"
mkdir -p "$KIT"
cp "$EXE" "$KIT/"
python3 - "$WORK/nssm.zip" "$KIT/nssm.exe" <<'PYEOF'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
name = next(n for n in z.namelist() if n.endswith("/win64/nssm.exe"))
open(sys.argv[2], "wb").write(z.read(name))
PYEOF
python3 - "$WORK/ch341.zip" "$KIT/driver" <<'PYEOF'
import os, sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
os.makedirs(sys.argv[2])
for name in z.namelist():
    if name.startswith("CH341SER/WIN 1X/") and not name.endswith("/"):
        open(os.path.join(sys.argv[2], os.path.basename(name)), "wb").write(z.read(name))
PYEOF
[[ -f "$KIT/driver/CH341SER.INF" ]] || { echo "No CH341SER.INF in the driver download" >&2; exit 1; }
# Windows needs CR LF line endings in .bat files.
for f in install.bat uninstall.bat README.md; do
    sed 's/\r\?$/\r/' "$HERE/$f" > "$KIT/$f"
done
echo "$VERSION" > "$KIT/VERSION.txt"
if [[ -n "${SERVER_IP:-}" ]]; then
    printf '%s\r\n' "$SERVER_IP" > "$KIT/server.txt"
fi
(cd "$KIT" && sha256sum ReaderBridge.exe nssm.exe > SHA256SUMS.txt)

mkdir -p "$OUT"
ZIP="$OUT/reader-bridge-kit-$VERSION.zip"
python3 - "$WORK" "reader-bridge-kit-$VERSION" "$ZIP" <<'PYEOF'
import os, sys, zipfile
root, kit, out = sys.argv[1:]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for folder, _dirs, files in sorted(os.walk(os.path.join(root, kit))):
        for name in sorted(files):
            path = os.path.join(folder, name)
            z.write(path, os.path.relpath(path, root))
PYEOF
(cd "$OUT" && sha256sum "$(basename "$ZIP")" > "$(basename "$ZIP").sha256")
echo "Built $ZIP"
