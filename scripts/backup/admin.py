#!/usr/bin/python3
"""backend-backup-admin: what the web app may do with the backups, nothing more.

Installed root-owned as /usr/local/sbin/backend-backup-admin and run as root by systemd
for each connection to /run/backend-backup-admin.sock (backend-backup-admin.socket, which
only the `backend` group may use). The web service keeps NoNewPrivileges: no sudo.

    backend-backup-admin status             JSON: settings, last and next run, files, log
    backend-backup-admin run                start the nightly backup now (in the background)
    backend-backup-admin set KEY=VALUE ...  BACKUP_TIME, KEEP_DAILY_DAYS, KEEP_BASE_BACKUPS,
                                            OFFSITE_DIR, OFFSITE_RSYNC
    backend-backup-admin download NAME      one nightly dump, to stdout
    backend-backup-admin upload             a dump from stdin, saved as uploaded-<stamp>.dump
    backend-backup-admin restore NAME USER  restore that dump (its own systemd job:
                                            restore_web.sh, which backs up first)
    backend-backup-admin drop-kept DBNAME   delete a database kept by an earlier restore
    backend-backup-admin --socket           the socket service: one request per connection

Socket protocol: the client sends one line, a JSON list of the arguments above; the answer
is one line {"exit": code}, then the output. For `upload`, the file follows the request
line until the client closes its side. Every value is checked strictly: backup.conf
is read by the backup scripts as root. Errors are JSON {"error": ..., "field": ...} with
exit code 2 (3: a backup or restore is running). Standard library only.
"""

import hashlib
import json
import os
import pwd
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

# Overridable for tests. The web app can't set them: systemd starts the socket service
# with its own fixed environment.
_ENV = os.environ
CONF = Path(_ENV.get("BACKUP_CONF", "/etc/backend/backup.conf"))
SYSTEMCTL = _ENV.get("BACKUP_SYSTEMCTL", "systemctl")
JOURNALCTL = _ENV.get("BACKUP_JOURNALCTL", "journalctl")
SYSTEMD_DIR = Path(_ENV.get("BACKUP_SYSTEMD_DIR", "/etc/systemd/system"))
SYSTEMD_RUN = _ENV.get("BACKUP_SYSTEMD_RUN", "systemd-run")
PSQL = shlex.split(_ENV.get("BACKUP_PSQL", "runuser -u postgres -- psql -X"))
DROPDB = shlex.split(_ENV.get("BACKUP_DROPDB", "runuser -u postgres -- dropdb"))
PG_RESTORE = _ENV.get("BACKUP_PG_RESTORE", "pg_restore")

DUMP_NAME = re.compile(r"^[A-Za-z0-9_]+-\d{8}-\d{6}\.dump$")
UNIT_NAME = re.compile(r"^[a-z0-9-]+$")
PATH_VALUE = re.compile(r"^/[A-Za-z0-9._/-]*$")
RSYNC_VALUE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:/[A-Za-z0-9._/-]*$")
TIME_VALUE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
USER_VALUE = re.compile(r"^[A-Za-z0-9_.@+-]{1,150}$")
DB_VALUE = re.compile(r"^[a-z0-9_]+$")
ACTIVE = ("activating", "active", "reloading", "deactivating")
SETTABLE = ("BACKUP_TIME", "KEEP_DAILY_DAYS", "KEEP_BASE_BACKUPS", "OFFSITE_DIR", "OFFSITE_RSYNC")


class Refused(Exception):
    def __init__(self, message, field=None, code=2):
        super().__init__(message)
        self.field = field
        self.code = code


def read_conf() -> dict:
    """KEY=VALUE lines of backup.conf (parsed, never executed)."""
    if not CONF.is_file():
        raise Refused(f"Missing {CONF}: install the kit first.")
    values = {}
    for line in CONF.read_text().splitlines():
        match = re.match(r"^\s*([A-Z_][A-Z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def write_conf(changes: dict) -> None:
    lines = CONF.read_text().splitlines()
    done = set()
    for i, line in enumerate(lines):
        match = re.match(r"^\s*([A-Z_][A-Z0-9_]*)=", line)
        if match and match.group(1) in changes:
            lines[i] = f"{match.group(1)}={changes[match.group(1)]}"
            done.add(match.group(1))
    lines += [f"{k}={v}" for k, v in changes.items() if k not in done]
    mode = CONF.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile("w", dir=CONF.parent, delete=False) as tmp:
        tmp.write("\n".join(lines) + "\n")
    os.chmod(tmp.name, mode)
    os.replace(tmp.name, CONF)


def units(conf: dict) -> tuple[str, str]:
    backup = conf.get("BACKUP_UNIT") or "backend-backup"
    base = conf.get("BASE_BACKUP_UNIT") or "backend-basebackup"
    for name in (backup, base, restore_unit(conf)):
        if not UNIT_NAME.match(name):
            raise Refused(f"Bad unit name in backup.conf: {name}")
    return backup, base


def restore_unit(conf: dict) -> str:
    return conf.get("RESTORE_UNIT") or "backend-restore"


def db_dir(conf: dict) -> Path:
    return Path(conf.get("BACKUP_DIR") or "/var/backups/backend") / "db"


def db_name(conf: dict) -> str:
    name = conf.get("DB_NAME") or "backend"
    if not DB_VALUE.match(name):
        raise Refused(f"Bad DB_NAME in backup.conf: {name}")
    return name


def busy(conf: dict) -> str | None:
    """ "backup" or "restore" while one runs."""
    backup, _base = units(conf)
    if show(f"{backup}.service", "ActiveState").get("ActiveState") in ACTIVE:
        return "backup"
    if show(f"{restore_unit(conf)}.service", "ActiveState").get("ActiveState") in ACTIVE:
        return "restore"
    return None


def kept_databases(conf: dict) -> list[str]:
    """Databases an earlier restore kept (<db>_before_restore_<stamp>), newest first."""
    pattern = f"{db_name(conf)}\\_before\\_restore\\_%"
    result = subprocess.run(
        [*PSQL, "-tAc", f"SELECT datname FROM pg_database WHERE datname LIKE '{pattern}'"],
        capture_output=True,
        text=True,
    )
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return sorted((n for n in names if DB_VALUE.match(n)), reverse=True)


def systemctl(*args: str, check=True) -> str:
    result = subprocess.run([SYSTEMCTL, *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise Refused((result.stderr or result.stdout).strip() or f"systemctl {args[0]} failed")
    return result.stdout


def show(unit: str, *props: str) -> dict:
    args = ["show", unit, "--timestamp=unix"] + [f"-p{p}" for p in props]
    out = systemctl(*args, check=False)
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def iso(stamp: str | None) -> str | None:
    """systemd's "@1791333281" as ISO 8601 UTC."""
    if not stamp or not stamp.startswith("@"):
        return None
    try:
        return datetime.fromtimestamp(int(stamp[1:]), UTC).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def schedule(timer: str) -> tuple[str | None, str | None]:
    """(HH:MM of a daily timer, next run) from TimersCalendar."""
    calendar = show(timer, "TimersCalendar").get("TimersCalendar", "")
    time = re.search(r"OnCalendar=\S*\s(\d{2}):(\d{2})", calendar)
    nxt = re.search(r"next_elapse=(@\d+)", calendar)
    return (
        f"{time.group(1)}:{time.group(2)}" if time else None,
        iso(nxt.group(1)) if nxt else None,
    )


def files(folder: Path, pattern: str) -> list[dict]:
    if not folder.is_dir():
        return []
    out = []
    for path in folder.glob(pattern):
        stat = path.stat()
        created = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat().replace("+00:00", "Z")
        out.append({"name": path.name, "bytes": stat.st_size, "created_at": created})
    return sorted(out, key=lambda f: f["created_at"], reverse=True)


def status() -> dict:
    conf = read_conf()
    backup, base = units(conf)
    service = show(
        f"{backup}.service",
        "ActiveState",
        "Result",
        "ExecMainStatus",
        "ExecMainStartTimestamp",
        "ExecMainExitTimestamp",
    )
    time, next_run = schedule(f"{backup}.timer")
    _base_time, base_next = schedule(f"{base}.timer")
    folder = Path(conf.get("BACKUP_DIR") or "/var/backups/backend")
    log = subprocess.run(
        [JOURNALCTL, "-u", f"{backup}.service", "-n", "40", "--no-pager", "-o", "short-iso"],
        capture_output=True,
        text=True,
    ).stdout
    try:
        saved = json.loads(Path(conf.get("STATUS_FILE") or "/nonexistent").read_text())
    except (OSError, ValueError):
        saved = {}
    restore = restore_unit(conf)
    restore_log = subprocess.run(
        [JOURNALCTL, "-u", f"{restore}.service", "-n", "60", "--no-pager", "-o", "short-iso"],
        capture_output=True,
        text=True,
    ).stdout
    base_dirs = []
    if (folder / "base").is_dir():
        for path in sorted((folder / "base").iterdir(), reverse=True):
            if path.is_dir():
                created = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                base_dirs.append(
                    {"name": path.name, "created_at": created.isoformat().replace("+00:00", "Z")}
                )
    return {
        "config": {
            "backup_time": conf.get("BACKUP_TIME") or time or "02:30",
            "keep_daily_days": int(conf.get("KEEP_DAILY_DAYS") or 14),
            "keep_base_backups": int(conf.get("KEEP_BASE_BACKUPS") or 4),
            "offsite_dir": conf.get("OFFSITE_DIR", ""),
            "offsite_rsync": conf.get("OFFSITE_RSYNC", ""),
            "backup_dir": str(folder),
        },
        "running": service.get("ActiveState") in ("activating", "active", "reloading"),
        "last_run": {
            "result": service.get("Result") or None,
            "exit_status": int(service.get("ExecMainStatus") or 0),
            "started_at": iso(service.get("ExecMainStartTimestamp")),
            "finished_at": iso(service.get("ExecMainExitTimestamp")),
        },
        "next_run": next_run,
        "base_next_run": base_next,
        "dumps": files(folder / "db", "*.dump"),
        "base_backups": base_dirs,
        "log": log.splitlines()[-40:],
        "restore": {
            "running": show(f"{restore}.service", "ActiveState").get("ActiveState") in ACTIVE,
            "file": saved.get("restore_file") or None,
            "by": saved.get("restore_by") or None,
            "started_at": saved.get("restore_started_at") or None,
            "finished_at": saved.get("restore_finished_at") or None,
            "ok": {"true": True, "false": False}.get(saved.get("restore_ok", "")),
            "error": saved.get("restore_error") or None,
            "kept_database": saved.get("restore_kept_db") or None,
            "log": restore_log.splitlines()[-60:],
        },
        "kept_databases": kept_databases(conf),
    }


def run() -> dict:
    conf = read_conf()
    backup, _base = units(conf)
    if running := busy(conf):
        raise Refused(f"A {running} is already running.", code=3)
    systemctl("start", "--no-block", f"{backup}.service")
    return {"started": True}


def _check(key: str, value: str) -> str:
    if key == "BACKUP_TIME":
        if not TIME_VALUE.match(value):
            raise Refused("Use a time like 02:30.", key)
    elif key in ("KEEP_DAILY_DAYS", "KEEP_BASE_BACKUPS"):
        top = 365 if key == "KEEP_DAILY_DAYS" else 52
        if not value.isdigit() or not 1 <= int(value) <= top:
            raise Refused(f"Use a whole number from 1 to {top}.", key)
        value = str(int(value))
    elif key == "OFFSITE_DIR":
        if value and (not PATH_VALUE.match(value) or ".." in value.split("/")):
            raise Refused("Use a full folder path, e.g. /mnt/backup-disk/backend.", key)
        if value and not Path(value).is_dir():
            raise Refused("This folder doesn't exist (is the disk mounted?).", key)
    elif key == "OFFSITE_RSYNC":
        if value and (not RSYNC_VALUE.match(value) or ".." in value):
            raise Refused("Use user@host:/path, e.g. backup@10.0.0.50:/srv/backups/backend.", key)
    else:
        raise Refused(f"{key} can't be changed here.", key)
    return value


def set_values(pairs: list[str]) -> dict:
    changes = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or key not in SETTABLE:
            raise Refused(f"Unknown setting: {key}", key)
        changes[key] = _check(key, value.strip())
    conf = read_conf()
    merged = {**conf, **changes}
    if merged.get("OFFSITE_DIR") and merged.get("OFFSITE_RSYNC"):
        raise Refused("Set an off-site folder or an rsync target, not both.", "OFFSITE_RSYNC")
    write_conf(changes)
    if "BACKUP_TIME" in changes:
        backup, _base = units(conf)
        dropin = SYSTEMD_DIR / f"{backup}.timer.d"
        dropin.mkdir(parents=True, exist_ok=True)
        (dropin / "schedule.conf").write_text(
            "# Written by backend-backup-admin (the web page's backup time).\n"
            f"[Timer]\nOnCalendar=\nOnCalendar=*-*-* {changes['BACKUP_TIME']}:00\n"
        )
        systemctl("daemon-reload")
        systemctl("restart", f"{backup}.timer")
    return {"saved": sorted(changes)}


def download(name: str):
    """The dump's bytes, in chunks (checked before the first one is sent)."""
    if not DUMP_NAME.match(name):
        raise Refused("Not a backup file name.", "name")
    folder = Path(read_conf().get("BACKUP_DIR") or "/var/backups/backend") / "db"
    path = folder / name
    if not path.is_file():
        raise Refused("No such backup file.", "name")
    source = open(path, "rb")  # noqa: SIM115 (closed by the generator)

    def chunks():
        with source:
            while chunk := source.read(1 << 20):
                yield chunk

    return chunks()


def _dump_path(conf: dict, name: str) -> Path:
    if not DUMP_NAME.match(name):
        raise Refused("Not a backup file name.", "name")
    path = db_dir(conf) / name
    if not path.is_file():
        raise Refused("No such backup file.", "name")
    return path


def restore(name: str, by: str) -> dict:
    """Start restore_web.sh as its own job: the web app stops while it runs."""
    conf = read_conf()
    _dump_path(conf, name)
    if not USER_VALUE.match(by):
        raise Refused("Bad user name.", "by")
    if running := busy(conf):
        raise Refused(f"A {running} is already running.", code=3)
    script = (
        Path(conf.get("SCRIPTS_DIR") or "/opt/backend/current/scripts/backup") / "restore_web.sh"
    )
    info = script.stat() if script.is_file() else None
    if info is None or info.st_uid != 0 or info.st_mode & 0o022:
        # Root runs it: refuse a script that anyone but root could have changed.
        raise Refused("The restore script is missing or not root-owned: reinstall the kit.")
    result = subprocess.run(
        [
            SYSTEMD_RUN,
            f"--unit={restore_unit(conf)}",
            f"--description=Restore {name} (Backups page, {by})",
            "--collect",
            str(script),
            name,
            by,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise Refused((result.stderr or "systemd-run failed").strip())
    return {"started": True, "file": name}


def upload(stream) -> dict:
    """Save a dump from `stream` as uploaded-<stamp>.dump, if pg_restore can read it."""
    conf = read_conf()
    limit = int(conf.get("UPLOAD_MAX_BYTES") or 2 * 1024**3)
    folder = db_dir(conf)
    folder.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=folder, prefix=".upload-", suffix=".partial")
    os.close(handle)
    partial = Path(name)
    digest, size = hashlib.sha256(), 0
    try:
        with open(partial, "wb") as out:
            os.chmod(partial, 0o640)
            first = True
            while chunk := stream.read(1 << 20):
                if first and not chunk.startswith(b"PGDMP"):
                    raise Refused(
                        "This isn't a database backup (.dump) made by this system.", "file"
                    )
                first = False
                size += len(chunk)
                if size > limit:
                    raise Refused(f"The file is larger than {limit // 1024**2} MB.", "file")
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise Refused("The file is empty.", "file")
        check = subprocess.run([PG_RESTORE, "--list", str(partial)], capture_output=True)
        if check.returncode != 0:
            raise Refused("pg_restore can't read this file: it is damaged or not a backup.", "file")
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    try:
        owner = pwd.getpwnam("postgres")
        os.chown(partial, owner.pw_uid, owner.pw_gid)
    except (KeyError, PermissionError):
        pass  # tests: no postgres user / not root
    while True:  # named by the time it was saved; one per second
        target = folder / f"uploaded-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.dump"
        if not target.exists():
            break
        time.sleep(1)
    os.replace(partial, target)
    (folder / f"{target.name}.sha256").write_text(f"{digest.hexdigest()}  {target}\n")
    return {"name": target.name, "bytes": size}


def drop_kept(name: str) -> dict:
    conf = read_conf()
    if name not in kept_databases(conf):
        raise Refused("No such kept database.", "name")
    if busy(conf) == "restore":
        raise Refused("A restore is running.", code=3)
    result = subprocess.run([*DROPDB, name], capture_output=True, text=True)
    if result.returncode != 0:
        raise Refused((result.stderr or "dropdb failed").strip())
    return {"dropped": name}


def handle(argv: list[str], stdin=None):
    """(exit code, output: bytes or an iterator of bytes) for one command."""
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if command == "status" and not args:
            result = status()
        elif command == "run" and not args:
            result = run()
        elif command == "set" and args:
            result = set_values(args)
        elif command == "download" and len(args) == 1:
            return 0, download(args[0])
        elif command == "upload" and not args:
            result = upload(stdin)
        elif command == "restore" and len(args) == 2:
            result = restore(args[0], args[1])
        elif command == "drop-kept" and len(args) == 1:
            result = drop_kept(args[0])
        else:
            raise Refused(
                "Usage: status | run | set KEY=VALUE... | download NAME | upload"
                " | restore NAME USER | drop-kept DBNAME"
            )
    except Refused as exc:
        return exc.code, json.dumps({"error": str(exc), "field": exc.field}).encode()
    return 0, json.dumps(result).encode()


def _write(out, data) -> None:
    for chunk in [data] if isinstance(data, bytes) else data:
        out.write(chunk)


def serve_socket() -> int:
    """One request on stdin (the connection), the answer on stdout."""
    line = sys.stdin.buffer.readline(8192)
    try:
        argv = json.loads(line)
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ValueError
    except ValueError:
        argv = []
    code, data = handle(argv, sys.stdin.buffer)
    out = sys.stdout.buffer
    out.write(json.dumps({"exit": code}).encode() + b"\n")
    _write(out, data)
    out.flush()
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--socket"]:
        return serve_socket()
    code, data = handle(argv, sys.stdin.buffer)
    _write(sys.stdout.buffer, data)
    if isinstance(data, bytes):
        sys.stdout.buffer.write(b"\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
