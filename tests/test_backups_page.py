"""The Backups page: status, run now, settings and downloads, through the root helper
(scripts/backup/admin.py) with a fake systemctl and a temporary backup folder."""

import json
import os
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog

ADMIN = Path(__file__).resolve().parent.parent / "scripts" / "backup" / "admin.py"
PAGE = "/api/v1/system/backups/"
DUMP = "backend-20261007-003441.dump"


@pytest.fixture
def box(tmp_path, monkeypatch, settings):
    """backup.conf, a backup folder with two dumps, and fake systemctl / journalctl."""
    backups = tmp_path / "backups"
    (backups / "db").mkdir(parents=True)
    (backups / "base" / "20261004-013001").mkdir(parents=True)
    (backups / "db" / DUMP).write_bytes(b"PGDMP" + b"x" * 100)
    (backups / "db" / "backend-20261006-003321.dump").write_bytes(b"PGDMP")
    os.utime(backups / "db" / "backend-20261006-003321.dump", (1791247000, 1791247000))
    os.utime(backups / "db" / DUMP, (1791333281, 1791333281))
    conf = tmp_path / "backup.conf"
    conf.write_text(
        f"# Backup settings\nBACKUP_DIR={backups}\nDB_NAME=backend\nKEEP_DAILY_DAYS=14\n"
        "KEEP_BASE_BACKUPS=4\nOFFSITE_DIR=\nOFFSITE_RSYNC=\n"
        f"STATUS_FILE={tmp_path / 'status.json'}\nSCRIPTS_DIR={tmp_path / 'scripts'}\n"
    )
    (tmp_path / "scripts").mkdir()
    restore_script = tmp_path / "scripts" / "restore_web.sh"
    restore_script.write_text("#!/bin/bash\n")
    restore_script.chmod(0o755)
    calls = tmp_path / "calls.log"
    state = tmp_path / "state"
    state.write_text("inactive")
    restore_state = tmp_path / "restore-state"
    restore_state.write_text("inactive")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> {calls}\n'
        'if [[ "$1" == show && "$2" == *.timer ]]; then\n'
        '  echo "TimersCalendar={ OnCalendar=*-*-* 02:30:00 ; next_elapse=@1791419400 }"\n'
        'elif [[ "$1" == show && "$2" == *restore* ]]; then\n'
        f'  echo "ActiveState=$(cat {restore_state})"\n'
        'elif [[ "$1" == show ]]; then\n'
        f'  echo "ActiveState=$(cat {state})"; echo Result=success; echo ExecMainStatus=0\n'
        "  echo ExecMainStartTimestamp=@1791333281; echo ExecMainExitTimestamp=@1791333284\n"
        "fi\n"
    )
    journalctl = tmp_path / "journalctl"
    journalctl.write_text("#!/bin/bash\necho 'nightly.sh: Done'\n")
    systemd_run = tmp_path / "systemd-run"
    systemd_run.write_text(f'#!/bin/bash\necho "systemd-run $*" >> {calls}\n')
    kept = tmp_path / "kept"
    kept.write_text("backend_before_restore_20261001_120000\n")
    psql = tmp_path / "psql"
    psql.write_text(f"#!/bin/bash\ncat {kept}\n")
    dropdb = tmp_path / "dropdb"
    dropdb.write_text(f'#!/bin/bash\necho "dropdb $*" >> {calls}\n: > {kept}\n')
    pg_restore = tmp_path / "pg_restore"
    pg_restore.write_text('#!/bin/bash\ngrep -q GOOD "$2"\n')  # a "readable" test dump says GOOD
    for tool in (systemctl, journalctl, systemd_run, psql, dropdb, pg_restore):
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("BACKUP_CONF", str(conf))
    monkeypatch.setenv("BACKUP_SYSTEMCTL", str(systemctl))
    monkeypatch.setenv("BACKUP_JOURNALCTL", str(journalctl))
    monkeypatch.setenv("BACKUP_SYSTEMD_DIR", str(tmp_path / "systemd"))
    monkeypatch.setenv("BACKUP_SYSTEMD_RUN", str(systemd_run))
    monkeypatch.setenv("BACKUP_PSQL", str(psql))
    monkeypatch.setenv("BACKUP_DROPDB", str(dropdb))
    monkeypatch.setenv("BACKUP_PG_RESTORE", str(pg_restore))
    settings.BACKUP_ADMIN_COMMAND = f"{sys.executable} {ADMIN}"

    class Box:
        pass

    b = Box()
    b.__dict__.update(
        conf=conf,
        calls=calls,
        state=state,
        restore_state=restore_state,
        root=tmp_path,
        backups=backups,
        restore_script=restore_script,
    )
    return b


@pytest.fixture
def admin(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN, username="root"))


def test_status_shows_settings_runs_files_and_health(box, admin):
    body = admin.get(PAGE).json()
    assert body["config"]["backup_time"] == "02:30"
    assert body["config"]["keep_daily_days"] == 14 and body["running"] is False
    assert body["last_run"] == {
        "result": "success",
        "exit_status": 0,
        "started_at": "2026-10-07T00:34:41Z",
        "finished_at": "2026-10-07T00:34:44Z",
    }
    assert body["next_run"] == "2026-10-08T00:30:00Z"
    assert [d["name"] for d in body["dumps"]] == [DUMP, "backend-20261006-003321.dump"]
    assert body["base_backups"][0]["name"] == "20261004-013001"
    assert body["log"] == ["nightly.sh: Done"]
    assert "errors" in body["health"]


def test_run_now_starts_the_service_once(box, admin):
    r = admin.post(PAGE + "run/")
    assert r.status_code == 202
    assert "start --no-block backend-backup.service" in box.calls.read_text()
    assert AuditLog.objects.filter(action="system.backup_started").exists()
    box.state.write_text("active")
    r = admin.post(PAGE + "run/")
    assert r.status_code == 409 and r.json()["error"]["code"] == "BACKUP_RUNNING"


def test_settings_are_checked_and_saved(box, admin):
    offsite = box.root / "usb"
    offsite.mkdir()
    r = admin.patch(
        PAGE,
        {"backup_time": "03:15", "keep_daily_days": 30, "offsite_dir": str(offsite)},
        format="json",
    )
    assert r.status_code == 200, r.json()
    conf = box.conf.read_text()
    assert "KEEP_DAILY_DAYS=30" in conf and f"OFFSITE_DIR={offsite}" in conf
    assert "BACKUP_TIME=03:15" in conf and conf.startswith("# Backup settings")
    dropin = box.root / "systemd" / "backend-backup.timer.d" / "schedule.conf"
    assert "OnCalendar=*-*-* 03:15:00" in dropin.read_text()
    assert "restart backend-backup.timer" in box.calls.read_text()
    log = AuditLog.objects.get(action="system.backup_settings_changed")
    assert log.old_values["keep_daily_days"] == 14 and log.new_values["keep_daily_days"] == "30"


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"backup_time": "25:00"}, "backup_time"),
        ({"keep_daily_days": 0}, "keep_daily_days"),
        ({"offsite_dir": "/no/such/disk"}, "offsite_dir"),
        ({"offsite_dir": "/tmp/x; rm -rf /"}, "offsite_dir"),
        ({"offsite_rsync": "backup@10.0.0.5:/srv/$(reboot)"}, "offsite_rsync"),
    ],
)
def test_bad_settings_are_refused(box, admin, body, field):
    before = box.conf.read_text()
    r = admin.patch(PAGE, body, format="json")
    assert r.status_code == 400
    assert field in r.json()["error"]["details"], r.json()
    assert box.conf.read_text() == before


def test_offsite_folder_and_rsync_not_both(box, admin):
    offsite = box.root / "usb"
    offsite.mkdir()
    body = {"offsite_dir": str(offsite), "offsite_rsync": "backup@10.0.0.5:/srv/b"}
    r = admin.patch(PAGE, body, format="json")
    assert r.status_code == 400 and "offsite_rsync" in r.json()["error"]["details"]


def test_download_one_dump(box, admin):
    r = admin.get(f"{PAGE}files/{DUMP}/")
    assert r.status_code == 200
    assert b"".join(r.streaming_content).startswith(b"PGDMP")
    assert r["Content-Disposition"] == f'attachment; filename="{DUMP}"'
    assert AuditLog.objects.get(action="system.backup_downloaded").new_values == {"file": DUMP}
    for name in ("backend-20990101-000000.dump", "..%2F..%2Fetc%2Fshadow", "backup.conf"):
        assert admin.get(f"{PAGE}files/{name}/").status_code == 404, name


@pytest.mark.parametrize("role", [Roles.BOSS, Roles.MANAGER, Roles.FINANCE_MANAGER])
def test_only_admins(box, auth_client, make_user, role):
    client = auth_client(make_user(role))
    assert client.get(PAGE).status_code == 403
    assert client.post(PAGE + "run/").status_code == 403
    assert client.get(f"{PAGE}files/{DUMP}/").status_code == 403
    assert "start" not in (box.calls.read_text() if box.calls.exists() else "")


def test_missing_helper_says_so(admin, settings):
    settings.BACKUP_ADMIN_COMMAND = "/no/such/backend-backup-admin"
    r = admin.get(PAGE)
    assert r.status_code == 503 and r.json()["error"]["code"] == "BACKUP_HELPER_UNAVAILABLE"


@pytest.fixture
def socket_helper(box, settings, tmp_path):
    """Like backend-backup-admin.socket: each connection is handed to the helper."""
    path = str(tmp_path / "admin.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen()
    server.settimeout(10)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                subprocess.run(
                    [sys.executable, str(ADMIN), "--socket"], stdin=conn, stdout=conn, check=False
                )

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    settings.BACKUP_ADMIN_COMMAND = ""
    settings.BACKUP_ADMIN_SOCKET = path
    yield
    server.close()


def test_through_the_socket(box, admin, socket_helper):
    body = admin.get(PAGE).json()
    assert body["config"]["keep_daily_days"] == 14 and body["dumps"][0]["name"] == DUMP
    r = admin.get(f"{PAGE}files/{DUMP}/")
    assert b"".join(r.streaming_content) == (box.backups / "db" / DUMP).read_bytes()
    r = admin.patch(PAGE, {"keep_daily_days": 0}, format="json")
    assert r.status_code == 400  # serializer: never reaches the helper
    r = admin.patch(PAGE, {"offsite_rsync": "nobody"}, format="json")
    assert r.status_code == 400 and "offsite_rsync" in r.json()["error"]["details"]
    box.state.write_text("active")
    assert admin.post(PAGE + "run/").status_code == 409


def test_socket_rejects_garbage(box, socket_helper, settings):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(settings.BACKUP_ADMIN_SOCKET)
    conn.sendall(b"rm -rf /\n")
    answer = conn.makefile("rb").read()
    conn.close()
    header, body = answer.split(b"\n", 1)
    assert json.loads(header) == {"exit": 2} and b"Usage" in body


# --- Restore and upload -------------------------------------------------------------

RESTORE = PAGE + "restore/"
root_only = pytest.mark.skipif(os.geteuid() != 0, reason="the helper wants a root-owned script")


@root_only
def test_restore_starts_its_own_job(box, admin):
    r = admin.post(RESTORE, {"file": DUMP, "confirm": "RESTORE"}, format="json")
    assert r.status_code == 202, r.json()
    call = [line for line in box.calls.read_text().splitlines() if "systemd-run" in line][0]
    assert "--unit=backend-restore" in call and "--collect" in call
    assert call.endswith(f"{box.restore_script} {DUMP} root")
    assert AuditLog.objects.get(action="system.restore_started").new_values == {"file": DUMP}


def test_restore_needs_the_phrase_and_a_known_file(box, admin):
    r = admin.post(RESTORE, {"file": DUMP, "confirm": "restore"}, format="json")
    assert r.json()["error"]["code"] == "RESTORE_NOT_CONFIRMED"
    r = admin.post(RESTORE, {"file": "backend-20990101-000000.dump", "confirm": "RESTORE"})
    assert r.status_code == 404
    assert "systemd-run" not in (box.calls.read_text() if box.calls.exists() else "")


@root_only
def test_no_restore_while_a_backup_or_restore_runs(box, admin):
    for state in (box.state, box.restore_state):
        state.write_text("active")
        r = admin.post(RESTORE, {"file": DUMP, "confirm": "RESTORE"}, format="json")
        assert r.status_code == 409, r.json()
        state.write_text("inactive")
    box.restore_state.write_text("active")
    assert admin.post(PAGE + "run/").status_code == 409  # and no backup during a restore


def test_restore_refuses_a_script_others_could_change(box, admin):
    box.restore_script.chmod(0o777)
    r = admin.post(RESTORE, {"file": DUMP, "confirm": "RESTORE"}, format="json")
    assert r.status_code == 400 and "root-owned" in r.json()["error"]["message"]


def test_status_shows_the_last_restore_and_kept_databases(box, admin):
    (box.root / "status.json").write_text(
        json.dumps(
            {
                "restore_file": DUMP,
                "restore_by": "root",
                "restore_ok": "true",
                "restore_kept_db": "backend_before_restore_20261001_120000",
                "restore_finished_at": "2026-10-07T05:00:00Z",
            }
        )
    )
    body = admin.get(PAGE).json()
    assert body["restore"]["ok"] is True and body["restore"]["file"] == DUMP
    assert body["restore"]["running"] is False
    assert body["kept_databases"] == ["backend_before_restore_20261001_120000"]


def test_delete_a_kept_database(box, admin):
    name = "backend_before_restore_20261001_120000"
    assert admin.delete(f"{PAGE}kept/{name}/").status_code == 204
    assert f"dropdb {name}" in box.calls.read_text()
    assert admin.delete(f"{PAGE}kept/backend/").status_code == 400  # only kept copies
    assert AuditLog.objects.filter(action="system.kept_database_deleted").count() == 1


def upload(client, content: bytes, name="old-server.dump"):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return client.post(
        PAGE + "upload/", {"file": SimpleUploadedFile(name, content)}, format="multipart"
    )


def test_upload_saves_a_readable_dump(box, admin):
    r = upload(admin, b"PGDMP GOOD backup")
    assert r.status_code == 201, r.json()
    name = r.json()["name"]
    assert name.startswith("uploaded-") and name.endswith(".dump")
    saved = box.backups / "db" / name
    assert saved.read_bytes() == b"PGDMP GOOD backup"
    assert (box.backups / "db" / f"{name}.sha256").read_text().endswith(f"  {saved}\n")
    assert name in {d["name"] for d in admin.get(PAGE).json()["dumps"]}  # restorable now
    log = AuditLog.objects.get(action="system.backup_uploaded")
    assert log.new_values == {"file": name, "original_name": "old-server.dump"}


@pytest.mark.parametrize(
    ("content", "says"),
    [(b"PK\x03\x04 a zip", "isn't a database backup"), (b"PGDMP but broken", "damaged")],
)
def test_upload_refuses_what_is_not_a_backup(box, admin, content, says):
    r = upload(admin, content)
    assert r.status_code == 400 and says in r.json()["error"]["message"]
    assert not [p for p in (box.backups / "db").iterdir() if p.name.startswith("uploaded")]


def test_upload_size_limit(box, admin):
    box.conf.write_text(box.conf.read_text() + "UPLOAD_MAX_BYTES=10\n")
    r = upload(admin, b"PGDMP GOOD and more than ten bytes")
    assert r.status_code == 400 and "larger" in r.json()["error"]["message"]


def test_upload_and_restore_through_the_socket(box, admin, socket_helper):
    r = upload(admin, b"PGDMP GOOD via socket")
    assert r.status_code == 201, r.json()
    assert (box.backups / "db" / r.json()["name"]).read_bytes() == b"PGDMP GOOD via socket"
    r = upload(admin, b"nonsense")
    assert r.status_code == 400


@pytest.mark.parametrize("role", [Roles.BOSS, Roles.MANAGER])
def test_restore_and_upload_only_for_admins(box, auth_client, make_user, role):
    client = auth_client(make_user(role))
    assert client.post(RESTORE, {"file": DUMP, "confirm": "RESTORE"}).status_code == 403
    assert upload(client, b"PGDMP GOOD").status_code == 403
    assert client.delete(f"{PAGE}kept/backend_before_restore_20261001_120000/").status_code == 403


def test_two_uploads_in_a_row_get_their_own_names(box, admin):
    first = upload(admin, b"PGDMP GOOD one").json()["name"]
    bad = upload(admin, b"not a dump")
    second = upload(admin, b"PGDMP GOOD two").json()["name"]
    assert bad.status_code == 400 and "isn't a database backup" in bad.json()["error"]["message"]
    assert first != second
    assert (box.backups / "db" / second).read_bytes() == b"PGDMP GOOD two"
