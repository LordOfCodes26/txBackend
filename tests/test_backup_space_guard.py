"""scripts/backup/common.sh `prune_wal` and `free_space_guard`: archived WAL is deleted up
to the oldest kept base backup (gzipped and plain files alike), and a nearly full backup
disk gives up the oldest history instead of letting the database stop."""

import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parent.parent / "scripts" / "backup" / "common.sh"
PG_BIN = next(iter(sorted(Path("/usr/lib/postgresql").glob("*/bin"))), None)


def base_backup(backups: Path, stamp: str, first_wal: str, age_days: int) -> None:
    folder = backups / "base" / stamp
    folder.mkdir(parents=True)
    label = f"START WAL LOCATION: 0/3000028 (file {first_wal})\n".encode()
    info = tarfile.TarInfo("backup_label")
    info.size = len(label)
    with tarfile.open(folder / "base.tar.gz", "w:gz") as tar:
        tar.addfile(info, io.BytesIO(label))
    old = time.time() - age_days * 86400
    os.utime(folder / "base.tar.gz", (old, old))
    os.utime(folder, (old, old))


@pytest.fixture
def backups(tmp_path):
    backups = tmp_path / "backups"
    for sub in ("base", "db", "wal"):
        (backups / sub).mkdir(parents=True)
    return backups


def run(tmp_path, backups, script, percent="", limit=85):
    """Run `script` after sourcing common.sh. `percent`: a bash expression standing in for
    how full the disk is (the real `df` can't be filled in a test)."""
    status = tmp_path / "status.json"
    conf = tmp_path / "backup.conf"
    conf.write_text(
        f"BACKUP_DIR={backups}\nDB_NAME=backend\nSTATUS_FILE={status}\nDISK_PRUNE_PERCENT={limit}\n"
    )
    fakes = 'as_postgres() { "$@"; }\n'
    if PG_BIN:
        fakes += f"PG_BIN={PG_BIN}\n"
    if percent:
        fakes += f"disk_percent() {{ echo $(( {percent} )); }}\n"
    done = subprocess.run(
        ["bash", "-c", f". {COMMON}\n{fakes}{script}"],
        env={**os.environ, "BACKUP_CONF": str(conf)},
        capture_output=True,
        text=True,
    )
    saved = json.loads(status.read_text()) if status.exists() else {}
    return done, saved


# A stand-in for how full the disk is: 10 % per base backup, 5 % per dump.
COUNT = "$(ls -1d {b}/base/*/ 2>/dev/null | wc -l) * 10 + $(ls -1 {b}/db/*.dump | wc -l) * 5"


@pytest.mark.skipif(PG_BIN is None, reason="needs PostgreSQL's pg_archivecleanup")
def test_prune_wal_keeps_what_the_oldest_base_backup_needs(tmp_path, backups):
    wal = backups / "wal"
    (wal / "000000010000000000000001").write_bytes(b"plain, from before gzip")
    for n in (2, 3, 4):
        (wal / f"00000001000000000000000{n}.gz").write_bytes(b"gz")
    (wal / "000000010000000000000001.00000028.backup.gz").write_bytes(b"history")
    old = time.time() - 9 * 86400
    os.utime(wal / "000000010000000000000001.00000028.backup.gz", (old, old))
    base_backup(backups, "20261019-033000", "000000010000000000000003", age_days=2)
    base_backup(backups, "20261020-033000", "000000010000000000000004", age_days=1)

    done, _ = run(tmp_path, backups, "prune_wal")

    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in wal.iterdir()) == [
        "000000010000000000000003.gz",
        "000000010000000000000004.gz",
    ]


@pytest.mark.skipif(PG_BIN is None, reason="needs PostgreSQL's pg_archivecleanup")
def test_a_nearly_full_disk_gives_up_the_oldest_base_backups(tmp_path, backups):
    for day, n in ((4, 1), (3, 2), (2, 3), (1, 4)):
        base_backup(backups, f"2026101{n}-033000", f"00000001000000000000000{n}", age_days=day)
        (backups / "wal" / f"00000001000000000000000{n}.gz").write_bytes(b"gz")
    for n in range(5):
        (backups / "db" / f"backend-2026101{n}-023000.dump").write_bytes(b"PGDMP")
    # 40 + 4 base backups (40) + 5 dumps (25) = 105 %: deleting 3 base backups gets to 75 %.
    percent = "40 + " + COUNT.format(b=backups)
    done, saved = run(tmp_path, backups, "free_space_guard", percent=percent)

    assert done.returncode == 0, done.stderr
    assert [p.name for p in (backups / "base").iterdir()] == ["20261014-033000"]
    assert [p.name for p in (backups / "wal").iterdir()] == ["000000010000000000000004.gz"]
    assert len(list((backups / "db").glob("*.dump"))) == 5  # dumps untouched
    assert saved["space_pruned_at"]
    assert "deleting base backup" in done.stderr


def test_then_the_oldest_dumps_but_never_the_newest_three(tmp_path, backups):
    base_backup(backups, "20261020-033000", "000000010000000000000004", age_days=1)
    for n in range(8):
        dump = backups / "db" / f"backend-2026101{n}-023000.dump"
        dump.write_bytes(b"PGDMP")
        Path(f"{dump}.sha256").write_text("x")
    # The disk stays full whatever is deleted: it stops at the newest base backup + 3 dumps.
    done, saved = run(tmp_path, backups, "free_space_guard", percent="99")

    assert done.returncode == 0, done.stderr
    assert sorted(p.name for p in (backups / "db").iterdir()) == [
        f"backend-2026101{n}-023000.dump{ext}" for n in (5, 6, 7) for ext in ("", ".sha256")
    ]
    assert [p.name for p in (backups / "base").iterdir()] == ["20261020-033000"]
    assert "still 99% full" in done.stderr
    assert saved["space_pruned_at"]


def test_a_disk_with_room_is_left_alone(tmp_path, backups):
    base_backup(backups, "20261019-033000", "000000010000000000000003", age_days=2)
    base_backup(backups, "20261020-033000", "000000010000000000000004", age_days=1)
    done, saved = run(tmp_path, backups, "free_space_guard", percent="60")
    assert done.returncode == 0, done.stderr
    assert len(list((backups / "base").iterdir())) == 2
    assert "space_pruned_at" not in saved and done.stderr == ""


def test_the_real_disk_percent_reads_df(tmp_path, backups):
    done, _ = run(tmp_path, backups, f"disk_percent {backups}")
    assert done.returncode == 0, done.stderr
    usage = shutil.disk_usage(backups)
    expected = 100 * usage.used / (usage.used + usage.free)
    assert abs(int(done.stdout) - expected) <= 1
