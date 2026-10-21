"""scripts/backup/common.sh `copy_offsite`: a backup disk that isn't mounted is refused
(and reported) instead of being copied onto this server's own disk."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parent.parent / "scripts" / "backup" / "common.sh"


@pytest.fixture
def setup(tmp_path):
    backups = tmp_path / "backups"
    (backups / "db").mkdir(parents=True)
    (backups / "db" / "backend-20261021-023000.dump").write_bytes(b"PGDMP")
    status = tmp_path / "status.json"

    def run(offsite_dir):
        conf = tmp_path / "backup.conf"
        conf.write_text(
            f"BACKUP_DIR={backups}\nDB_NAME=backend\nSTATUS_FILE={status}\n"
            f"OFFSITE_DIR={offsite_dir}\nOFFSITE_RSYNC=\n"
        )
        done = subprocess.run(
            ["bash", "-c", f". {COMMON} && copy_offsite"],
            env={**os.environ, "BACKUP_CONF": str(conf)},
            capture_output=True,
            text=True,
        )
        return done, json.loads(status.read_text())

    return tmp_path, run


def test_an_unmounted_backup_disk_is_refused(setup):
    tmp_path, run = setup
    # /mnt/usb-like mount point, not mounted: it (or its parent) is on this disk.
    offsite = tmp_path / "mnt" / "usb" / "backend"
    done, status = run(offsite)
    assert done.returncode != 0
    assert "is the backup disk mounted?" in done.stderr
    assert status["offsite_error"] == "NOT_MOUNTED"
    assert status["offsite_target"] == str(offsite) and "offsite_copy_at" not in status
    assert not (tmp_path / "mnt").exists()  # nothing was copied onto this disk
    # An existing (empty) mount point is refused the same way.
    (tmp_path / "mnt" / "usb").mkdir(parents=True)
    done, status = run(tmp_path / "mnt" / "usb")
    assert done.returncode != 0 and status["offsite_error"] == "NOT_MOUNTED"
    assert list((tmp_path / "mnt" / "usb").iterdir()) == []


def test_a_mounted_backup_disk_gets_the_copy(setup):
    tmp_path, run = setup
    if os.stat("/dev/shm").st_dev == os.stat(str(tmp_path)).st_dev or not shutil.which("rsync"):
        pytest.skip("needs rsync and /dev/shm on its own filesystem")
    disk = Path(tempfile.mkdtemp(dir="/dev/shm", prefix="offsite-"))
    try:
        run(tmp_path / "not-mounted")  # an earlier refusal is cleared by a good copy
        done, status = run(disk / "backend")
        assert done.returncode == 0, done.stderr
        assert (disk / "backend" / "db" / "backend-20261021-023000.dump").exists()
        assert status["offsite_error"] == "" and status["offsite_copy_at"]
        assert status["backup_dir"] == str(tmp_path / "backups")
    finally:
        shutil.rmtree(disk, ignore_errors=True)
