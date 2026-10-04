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
    # The Caddyfile is rendered in common.ps1 (also used by change-web-port.bat).
    installer = (WINDOWS / "install-all.ps1").read_text() + (WINDOWS / "common.ps1").read_text()
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


def test_redis_clients_use_resp2_for_garnet():
    # redis-py 8 defaults to RESP3 and CLIENT MAINT_NOTIFICATIONS; Garnet hangs on that.
    # (Test settings replace CACHES/CHANNEL_LAYERS with in-memory backends.)
    assert settings.REDIS_CONNECTION_KWARGS.get("protocol") == 2
    base = (pathlib.Path(settings.BASE_DIR) / "config" / "settings" / "base.py").read_text()
    assert 'REDIS_CONNECTION_KWARGS = {"protocol": 2}' in base
    assert "CONNECTION_POOL_KWARGS" in base
    assert '{"address": REDIS_URL, **REDIS_CONNECTION_KWARGS}' in base


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
    # Scripts must live outside backend\\current: deploy retargets that junction.
    assert 'DeployTools = "$Root\\deploy"' in installer or "$Root\\deploy\\deploy-dev.ps1" in installer
    assert "backend\\current\\deploy\\windows\\deploy-dev.ps1" not in installer


def test_deploys_use_committed_code_and_production_settings():
    deploy = (WINDOWS / "deploy-dev.ps1").read_text()
    assert "'archive', '--format=tar'" in deploy  # committed files only
    assert "DJANGO_SETTINGS_MODULE = 'config.settings.prod'" in deploy
    assert "backup.ps1" in deploy  # safety backup before migrating
    assert "Sync-DeployTools" in deploy
    assert "not all services came back" in deploy


def test_stop_service_does_not_abort_on_timeout():
    # WaitForStatus used to throw under ErrorActionPreference Stop and leave services down.
    common = (WINDOWS / "common.ps1").read_text()
    assert "did not stop in time" in common
    assert "Never throw" in common or "must not abort" in common


def test_port_checks_can_target_127_0_0_1():
    # Cursor listens on ::1:6379; a bare port check treated Garnet as up while it was dead.
    common = (WINDOWS / "common.ps1").read_text()
    assert "LocalAddress" in common
    assert "Test-RedisPing" in common
    installer = (WINDOWS / "install-all.ps1").read_text()
    assert "LocalAddress '127.0.0.1'" in installer
    assert "Test-RedisPing" in installer


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


def test_the_door_port_comes_from_the_settings():
    # A changed RFID_TCP_PORT must not make deploys roll back or the installer wait for 9100.
    for name in ("install-all.ps1", "deploy-dev.ps1"):
        text = (WINDOWS / name).read_text()
        hard = [
            line
            for line in text.splitlines()
            if "9100" in line and "DoorPortNow = 9100" not in line
        ]
        assert all(
            line.lstrip().startswith(("#", "The TCP port", "installation")) for line in hard
        ), hard
    assert (WINDOWS / "door-port.ps1").exists()
    assert "change-door-port.bat" in (WINDOWS / "install-all.ps1").read_text()


def test_the_web_ports_come_from_the_settings():
    # Re-running install.cmd used to go back to 80/443, and deploys checked https://localhost/.
    installer = (WINDOWS / "install-all.ps1").read_text()
    assert "[int]$HttpPort = 0," in installer and "[int]$HttpsPort = 0," in installer
    assert "$web = Get-WebPorts $EnvFile" in installer
    assert "'WEB_HTTPS_PORT'" in installer
    assert "https://localhost/" not in (WINDOWS / "deploy-dev.ps1").read_text()
    assert (WINDOWS / "web-port.ps1").exists()
    assert "change-web-port.bat" in installer
