#!/usr/bin/env bash
# Build DeviceSimulator.exe on Linux (no Windows needed), with Wine and the official
# Windows Python. Needs internet and: apt-get install --no-install-recommends wine64 msitools
#
#   tools/device-simulator/build_exe.sh [OUTPUT_DIR]     (default: dist/)
#
# Python 3.12 for Windows comes from NuGet (no installer: its setup program is 32-bit), its
# window toolkit from python.org's tcltk.msi, then PyInstaller builds one windowed .exe.
set -euo pipefail
exec </dev/null

PY_VERSION=3.12.10
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$(realpath -m "${1:-$HERE/dist}")"
WORK="$(mktemp -d)"
trap '/usr/lib/wine/wineserver64 -k 2>/dev/null || true; rm -rf "$WORK"' EXIT
export WINEPREFIX="$WORK/wine" WINEDEBUG=-all
WINE=/usr/lib/wine/wine64
[[ -x "$WINE" ]] || { echo "Install wine64 first (apt-get install --no-install-recommends wine64)" >&2; exit 1; }
command -v msiextract >/dev/null || { echo "Install msitools first" >&2; exit 1; }

echo "==> Windows Python $PY_VERSION"
curl -fsSL -o "$WORK/python.nupkg" "https://api.nuget.org/v3-flatcontainer/python/$PY_VERSION/python.$PY_VERSION.nupkg"
curl -fsSL -o "$WORK/tcltk.msi" "https://www.python.org/ftp/python/$PY_VERSION/amd64/tcltk.msi"
xvfb-run -a "$WINE" wineboot -i </dev/null >/dev/null 2>&1 || true
# Windows Python under Wine can't write straight to a file (no terminal, e.g. cron/CI):
# its output always goes through a pipe.
wpy() { "$WINE" 'C:\Py\python.exe' "$@" </dev/null 2>&1 | cat; }
PY="$WINEPREFIX/drive_c/Py"
python3 - "$WORK/python.nupkg" "$PY" <<'PYEOF'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
for name in z.namelist():
    if name.startswith("tools/") and not name.endswith("/"):
        target = sys.argv[2] + "/" + name[len("tools/"):]
        import os; os.makedirs(os.path.dirname(target), exist_ok=True)
        open(target, "wb").write(z.read(name))
PYEOF
(cd "$WORK" && mkdir tk && cd tk && msiextract ../tcltk.msi >/dev/null)
cp -r "$WORK/tk/Lib/tkinter" "$PY/Lib/"
cp "$WORK/tk/DLLs/"*.dll "$WORK/tk/DLLs/_tkinter.pyd" "$PY/DLLs/"
cp -r "$WORK/tk/tcl" "$PY/"
wpy -c "import tkinter" || { echo "tkinter missing" >&2; exit 1; }

echo "==> PyInstaller"
wpy -m pip install -q --no-warn-script-location pyinstaller
mkdir -p "$WINEPREFIX/drive_c/build"
cp "$HERE/device_simulator.py" "$WINEPREFIX/drive_c/build/"
wpy -m PyInstaller --noconfirm --onefile --windowed --name DeviceSimulator \
    --distpath 'C:\build\dist' --workpath 'C:\build\work' --specpath 'C:\build' \
    'C:\build\device_simulator.py' | tail -2

mkdir -p "$OUT"
cp "$WINEPREFIX/drive_c/build/dist/DeviceSimulator.exe" "$OUT/"
(cd "$OUT" && sha256sum DeviceSimulator.exe > DeviceSimulator.exe.sha256)
echo "Built $OUT/DeviceSimulator.exe"
