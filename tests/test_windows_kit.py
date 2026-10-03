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
