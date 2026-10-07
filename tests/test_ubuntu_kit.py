"""The Ubuntu kit's `mgmt` command (deploy/mgmt.sh): checks that run here; the commands
themselves are exercised on a test Ubuntu machine when the kit changes."""

import os
import pathlib
import shutil
import subprocess

import pytest
from django.conf import settings

BACKEND = pathlib.Path(settings.BASE_DIR)
MGMT = BACKEND / "deploy" / "mgmt.sh"


def test_mgmt_is_executable_and_parses():
    assert os.access(MGMT, os.X_OK)
    subprocess.run(["bash", "-n", str(MGMT)], check=True)


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
def test_mgmt_passes_shellcheck():
    subprocess.run(["shellcheck", "-S", "warning", str(MGMT)], check=True)


def test_mgmt_help_lists_the_commands():
    out = subprocess.run(["bash", str(MGMT), "--help"], capture_output=True, text=True).stdout
    for cmd in (
        "deploy-backend",
        "deploy-frontend",
        "deploy-all",
        "change-door-port",
        "change-web-port",
        "uninstall",
        "status",
    ):
        assert cmd in out


def test_installer_links_mgmt_and_keeps_the_web_ports():
    installer = (BACKEND / "scripts" / "install_offline.sh").read_text()
    assert 'ln -sfn "${BASE}/current/deploy/mgmt.sh" /usr/local/sbin/mgmt' in installer
    # nginx's site is rewritten from the release on every install: the saved ports go back in.
    assert '"${RELEASE}/deploy/mgmt.sh" apply-web-ports' in installer
    for script in ("deploy/install-all.sh", "deploy/install-backend.sh"):
        assert "WEB_HTTPS_PORT" in (BACKEND / script).read_text(), script


def test_apply_web_ports_rewrites_the_nginx_site(tmp_path):
    site = tmp_path / "backend.conf"
    site.write_text((BACKEND / "deploy" / "nginx" / "backend.conf").read_text())
    env = tmp_path / "backend.env"
    env.write_text("WEB_HTTP_PORT=8080\nWEB_HTTPS_PORT=8443\n")
    # Run only the function, with the paths pointed at the copies.
    script = MGMT.read_text().split("\nCMD=${1:-}")[0]
    snippet = (
        f"{script}\nENV_FILE={env}\nNGINX_SITE={site}\n"
        'apply_web_ports "$(http_port)" "$(https_port)"\n'
    )
    subprocess.run(["bash", "-c", snippet], check=True)
    text = site.read_text()
    assert "listen 8080 default_server;" in text
    assert "listen 8443 ssl default_server;" in text
    assert "return 301 https://$host:8443$request_uri;" in text
    assert "listen 127.0.0.1:8001;" in text  # the internal address is left alone


@pytest.mark.parametrize("port", ["8000", "3000", "9101"])
def test_port_changes_refuse_the_development_ports(port):
    # Free until a developer starts the development servers, then they would clash.
    assert port in MGMT.read_text().split("OWN_PORTS=(")[1].split(")")[0].split()
