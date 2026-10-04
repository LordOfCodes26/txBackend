"""The Windows kit's scripts (deploy/windows) can't run here, so these keep the things that
broke or would break them on Windows from coming back."""

import pathlib
import re

import pytest
from django.conf import settings

WINDOWS = pathlib.Path(settings.BASE_DIR) / "deploy" / "windows"
SCRIPTS = sorted(WINDOWS.glob("*.ps1")) + sorted(WINDOWS.glob("*.cmd"))
TEMPLATES = sorted(WINDOWS.glob("*.template"))


@pytest.mark.parametrize("path", SCRIPTS + TEMPLATES, ids=lambda p: p.name)
def test_ascii_only(path):
    # Windows PowerShell 5.1 reads a .ps1 without a byte order mark in the PC's legacy code
    # page: any non-ASCII character (an em dash in a comment) turns into garbage.
    bad = [n for n, line in enumerate(path.read_bytes().splitlines(), 1) if not line.isascii()]
    assert bad == [], f"{path.name}: non-ASCII characters on lines {bad}"


def test_install_cmd_has_windows_line_endings():
    data = (WINDOWS / "install.cmd").read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n"), "cmd.exe needs CRLF line endings"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_every_placeholder_is_filled_by_the_installer(path):
    installer = (WINDOWS / "install-all.ps1").read_text()
    for name in set(re.findall(r"__([A-Z0-9_]+)__", path.read_text())):
        assert re.search(rf"\b{name}\s*=", installer), f"{path.name}: nothing sets {name}"


def test_windows_settings_use_forward_slashes():
    # django-environ reads backslashes in values as escapes; Python accepts C:/ paths.
    for line in (WINDOWS / "backend.env.template").read_text().splitlines():
        if re.match(r"^[A-Z_]+=", line):
            assert "\\" not in line, line


def test_windows_settings_turn_off_point_in_time_backups():
    lines = (WINDOWS / "backend.env.template").read_text().splitlines()
    assert "BACKUP_REQUIRE_PITR=false" in lines


def test_services_get_the_production_settings_module():
    # manage.py defaults to the development settings; the services must not.
    installer = (WINDOWS / "install-all.ps1").read_text()
    assert "DJANGO_SETTINGS_MODULE = 'config.settings.prod'" in installer


def test_waitress_keeps_the_proxy_headers():
    # waitress deletes X-Forwarded-For/-Proto by default: every request would look like
    # plain HTTP from 127.0.0.1 (redirect loop, no till reader matching by address).
    installer = (WINDOWS / "install-all.ps1").read_text()
    assert "-m waitress" in installer
    assert "--no-clear-untrusted-proxy-headers" in installer


def test_windows_requirements():
    req = pathlib.Path(settings.BASE_DIR) / "requirements"
    lines = (req / "windows.txt").read_text().splitlines()
    assert not [x for x in lines if x.lower().startswith("gunicorn")], "gunicorn needs Unix"
    pins = (req / "constraints.txt").read_text().splitlines()
    for name in ("waitress", "tzdata"):
        assert any(p.lower().startswith(f"{name}==") for p in pins), f"{name} isn't pinned"


def test_garnet_booleans_have_values():
    # Garnet rejects a bare boolean flag ("--quiet") and exits at once: the cache was down.
    installer = (WINDOWS / "install-all.ps1").read_text()
    garnet = next(line for line in installer.splitlines() if "--bind 127.0.0.1 --port" in line)
    assert "--lua true" in garnet
    assert "--quiet" not in garnet


def test_winsw_services_run_as_local_system():
    # Under LocalService WinSW can't report its program's exit to Windows: a crashed program
    # left the service "Running" and was never restarted.
    common = (WINDOWS / "common.ps1").read_text()
    assert "Set-ServiceAccount $Id 'LocalSystem'" in common


def test_no_strict_mode():
    # Windows PowerShell 5.1 + Set-StrictMode: `.Count` of a single value is fatal ("Count is
    # unknown"), e.g. on a PC with one IP address. PowerShell 7 (used to check here) allows it.
    for path in SCRIPTS:
        assert "Set-StrictMode -Version" not in path.read_text(), path.name


def test_ip_candidates_stay_a_list():
    # A one-item list comes back as the bare item: with one IP, $ips[0] was its first character.
    installer = (WINDOWS / "install-all.ps1").read_text()
    assert "$ips = @(Get-ServerIPv4Candidates)" in installer


def test_installer_writes_the_deploy_bat_files():
    installer = (WINDOWS / "install-all.ps1").read_text()
    for bat in ("deploy-backend.bat", "deploy-frontend.bat", "deploy-all.bat"):
        assert bat in installer
    assert (WINDOWS / "deploy-dev.ps1").exists()


def test_deploys_use_committed_code_and_production_settings():
    deploy = (WINDOWS / "deploy-dev.ps1").read_text()
    assert "'archive', '--format=tar'" in deploy  # committed files only
    assert "DJANGO_SETTINGS_MODULE = 'config.settings.prod'" in deploy
    assert "backup.ps1" in deploy  # safety backup before migrating


@pytest.mark.parametrize("path", sorted(WINDOWS.glob("*.bat")), ids=lambda p: p.name)
def test_bat_files_have_windows_line_endings(path):
    data = path.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n"), f"{path.name}: cmd.exe needs CRLF"


def test_uninstall_bat_runs_from_a_temporary_copy():
    # -RemoveData deletes C:\Management, where this .bat may be: cmd reads a batch file line by
    # line, so it runs a copy, and its last command is a single line.
    bat = (WINDOWS / "uninstall.bat").read_text()
    assert "%TEMP%\\mgmt-uninstall" in bat
    last = [line for line in bat.splitlines() if line.strip()][-1]
    assert last.startswith("powershell") and last.endswith("& pause & exit /b")
    installer = (WINDOWS / "install-all.ps1").read_text()
    assert 'uninstall.bat"' in installer
    builder = (WINDOWS.parent.parent / "scripts" / "build_windows_kit.sh").read_text()
    assert "uninstall.bat" in builder
