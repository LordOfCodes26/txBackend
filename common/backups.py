"""The Backups page: talks to the root helper `backend-backup-admin` (scripts/backup/admin.py).

The web service runs as an unprivileged user (with NoNewPrivileges); the backups belong to
root and postgres. systemd runs the helper as root for each connection to its socket
(BACKUP_ADMIN_SOCKET, only for the `backend` group). It only reports status, starts the
nightly backup, changes checked settings, sends one named dump, saves an uploaded dump,
starts a restore (as its own systemd job) and deletes databases kept by a restore. Where
the web service already runs as root (staging, tests), BACKUP_ADMIN_COMMAND runs the
helper directly.
"""

import json
import re
import shlex
import socket
import subprocess

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import status

from common.exceptions import DomainError

DUMP_NAME = re.compile(r"^[A-Za-z0-9_]+-\d{8}-\d{6}\.dump$")
# API field -> backup.conf key
SETTINGS = {
    "backup_time": "BACKUP_TIME",
    "keep_daily_days": "KEEP_DAILY_DAYS",
    "keep_base_backups": "KEEP_BASE_BACKUPS",
    "offsite_dir": "OFFSITE_DIR",
    "offsite_rsync": "OFFSITE_RSYNC",
}


class BackupHelperUnavailable(DomainError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "BACKUP_HELPER_UNAVAILABLE"
    default_detail = _("The backup helper isn't installed or didn't answer. Reinstall the kit.")


class BackupRefused(DomainError):
    code = "BACKUP_REFUSED"


class BackupRunning(DomainError):
    status_code = status.HTTP_409_CONFLICT
    code = "BACKUP_RUNNING"
    default_detail = _("A backup is already running.")


def _command() -> list[str]:
    return shlex.split(settings.BACKUP_ADMIN_COMMAND)


def _connect(args) -> tuple[int, socket.socket]:
    """Send one request to the helper's socket; (exit code, the connection)."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(60)
    conn.connect(settings.BACKUP_ADMIN_SOCKET)
    conn.sendall(json.dumps(list(args)).encode() + b"\n")
    header = b""
    while not header.endswith(b"\n"):
        byte = conn.recv(1)
        if not byte:
            raise ValueError("no answer")
        header += byte
    return int(json.loads(header)["exit"]), conn


def _run(args) -> tuple[int, str]:
    if settings.BACKUP_ADMIN_COMMAND:
        result = subprocess.run([*_command(), *args], capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout
    code, conn = _connect(args)
    with conn, conn.makefile("rb") as stream:
        return code, stream.read().decode()


def _check(code: int, body) -> None:
    if code == 3:
        raise BackupRunning()
    if code == 2 and isinstance(body, dict) and "error" in body:
        field = body.get("field") if body.get("field") in ("file", "name") else None
        field = next((k for k, v in SETTINGS.items() if v == body.get("field")), field)
        raise BackupRefused(body["error"], details={field: [body["error"]]} if field else None)
    if code != 0 or not isinstance(body, dict):
        raise BackupHelperUnavailable()


def call(*args: str) -> dict:
    try:
        code, out = _run(args)
        body = json.loads(out or "null")
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        raise BackupHelperUnavailable() from exc
    _check(code, body)
    return body


def upload_dump(upload) -> dict:
    """Pass an uploaded file to the helper, which saves it as uploaded-<stamp>.dump."""
    try:
        if settings.BACKUP_ADMIN_COMMAND:
            process = subprocess.Popen(
                [*_command(), "upload"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
            )
            for chunk in upload.chunks():
                process.stdin.write(chunk)
            out, _ = process.communicate(timeout=600)
            code = process.returncode
        else:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(600)
            conn.connect(settings.BACKUP_ADMIN_SOCKET)
            with conn:
                conn.sendall(b'["upload"]\n')
                for chunk in upload.chunks():
                    conn.sendall(chunk)
                conn.shutdown(socket.SHUT_WR)
                with conn.makefile("rb") as answer:
                    header = json.loads(answer.readline())
                    code, out = int(header["exit"]), answer.read()
        body = json.loads(out or b"null")
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        raise BackupHelperUnavailable() from exc
    _check(code, body)
    return body


def start_restore(name: str, username: str) -> dict:
    return call("restore", name, username)


def drop_kept_database(name: str) -> dict:
    return call("drop-kept", name)


def backup_status() -> dict:
    return call("status")


def start_backup() -> dict:
    return call("run")


def change_settings(values: dict) -> dict:
    pairs = [f"{SETTINGS[key]}={value}" for key, value in values.items() if key in SETTINGS]
    return call("set", *pairs) if pairs else {"saved": []}


def open_dump(name: str):
    """The dump's bytes, in chunks. Refusals are raised before the first chunk."""
    if not DUMP_NAME.match(name):
        raise BackupRefused(_("Not a backup file name."))
    try:
        if settings.BACKUP_ADMIN_COMMAND:
            process = subprocess.Popen([*_command(), "download", name], stdout=subprocess.PIPE)
            stream, closer = process.stdout, process.wait
        else:
            code, conn = _connect(["download", name])
            if code != 0:
                with conn, conn.makefile("rb") as answer:
                    _check(code, json.loads(answer.read() or b"null"))
            stream, closer = conn.makefile("rb"), conn.close
    except (OSError, ValueError, KeyError) as exc:
        raise BackupHelperUnavailable() from exc

    def chunks():
        try:
            while chunk := stream.read(1 << 20):
                yield chunk
        finally:
            stream.close()
            closer()

    return chunks()
