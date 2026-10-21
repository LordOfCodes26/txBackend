import json
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def status_file(tmp_path, settings):
    path = tmp_path / "status.json"
    settings.BACKUP_STATUS_FILE = str(path)

    def write(**values):
        path.write_text(json.dumps(values))

    return write


def iso(delta):
    return (datetime.now(UTC) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


def healthy(**overrides):
    values = {
        "last_dump_at": iso(timedelta(hours=3)),
        "last_verify_ok": "true",
        "last_base_backup_at": iso(timedelta(days=2)),
        "offsite_target": "/mnt/nas",
        "offsite_copy_at": iso(timedelta(hours=3)),
    }
    return values | overrides


@pytest.mark.django_db
def test_no_status_file_is_an_error(client, settings, tmp_path):
    settings.BACKUP_STATUS_FILE = str(tmp_path / "missing.json")
    r = client.get("/health/backup/")
    assert r.status_code == 503
    assert "no backup status yet" in r.json()["errors"][0]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"last_dump_at": iso(timedelta(hours=30))}, "last database dump is missing or too old"),
        ({"last_verify_ok": "false"}, "last dump failed its restore check"),
        (
            {"last_base_backup_at": iso(timedelta(days=9))},
            "base backup for point-in-time recovery is missing or too old",
        ),
    ],
)
def test_stale_or_unverified_backups_are_errors(client, status_file, overrides, error):
    status_file(**healthy(**overrides))
    r = client.get("/health/backup/")
    assert r.status_code == 503
    assert error in r.json()["errors"]


@pytest.mark.django_db
def test_missing_offsite_copy_is_a_warning(client, status_file):
    status_file(**healthy(offsite_target=""))
    body = client.get("/health/backup/").json()
    assert "no off-site copy configured" in body["warnings"][0]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        (
            {"offsite_error": "NOT_MOUNTED"},
            "off-site copy refused: the backup disk isn't mounted",
        ),
        ({"offsite_error": "COPY_FAILED"}, "off-site copy failed"),
        ({"offsite_copy_at": iso(timedelta(hours=30))}, "off-site copy is missing or too old"),
        ({"offsite_copy_at": ""}, "off-site copy is missing or too old"),
    ],
)
def test_offsite_copy_not_made_is_an_error(client, status_file, settings, overrides, error):
    settings.BACKUP_REQUIRE_PITR = False
    status_file(**healthy(**overrides))
    r = client.get("/health/backup/")
    assert r.status_code == 503
    assert r.json()["errors"] == [error]


@pytest.fixture
def full_disk(monkeypatch):
    """Make every disk look `percent` full."""
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")

    def fill(percent):
        monkeypatch.setattr(
            shutil, "disk_usage", lambda path: usage(1000, percent * 10, 1000 - percent * 10)
        )

    return fill


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("percent", "code", "errors", "warnings"),
    [
        (79, 200, [], []),
        (80, 200, [], ["backups disk is 80% full"]),
        (89, 200, [], ["backups disk is 89% full"]),
        (90, 503, ["backups disk is 90% full"], []),
        (97, 503, ["backups disk is 97% full"], []),
    ],
)
def test_backup_disk_space(
    client, status_file, settings, tmp_path, full_disk, percent, code, errors, warnings
):
    settings.BACKUP_REQUIRE_PITR = False
    status_file(**healthy(backup_dir=str(tmp_path), offsite_target="backup@nas:/srv/b"))
    full_disk(percent)
    r = client.get("/health/backup/")
    assert r.status_code == code, r.json()
    assert r.json()["errors"] == errors and r.json()["warnings"] == warnings
    # Only the role is shown (the endpoint is public), not the folder.
    assert r.json()["disks"] == [
        {"role": "backups", "used_percent": float(percent), "free_bytes": 1000 - percent * 10}
    ]
    assert str(tmp_path) not in r.content.decode()


@pytest.mark.django_db
def test_offsite_disk_is_checked_too(client, status_file, settings, tmp_path):
    import os
    import tempfile

    if os.stat("/dev/shm").st_dev == os.stat(str(tmp_path)).st_dev:
        pytest.skip("needs /dev/shm on its own filesystem")
    settings.BACKUP_REQUIRE_PITR = False
    with tempfile.TemporaryDirectory(dir="/dev/shm") as offsite:
        status_file(**healthy(backup_dir=str(tmp_path), offsite_target=offsite))
        roles = [disk["role"] for disk in client.get("/health/backup/").json()["disks"]]
    assert roles == ["backups", "off-site"]


@pytest.mark.django_db
def test_reports_wal_archiver_state(client, status_file):
    status_file(**healthy())
    body = client.get("/health/backup/").json()
    assert "archive_mode" in body["wal_archiver"]
    # The test database server may or may not archive; either way it is reported.
    if body["wal_archiver"]["archive_mode"] != "on":
        assert "WAL archiving is off (no point-in-time recovery)" in body["errors"]


@pytest.mark.django_db
def test_without_pitr_only_nightly_dumps_are_required(client, status_file, settings):
    # Verified nightly dumps only, no base backups or WAL archiving.
    settings.BACKUP_REQUIRE_PITR = False
    status_file(**healthy(last_base_backup_at=""))
    r = client.get("/health/backup/")
    assert r.status_code == 200, r.json()
    assert r.json()["errors"] == []


def test_redis_failure_is_logged_with_its_reason(client, settings, caplog):
    # The endpoint only says "error"; the log says why.
    settings.REDIS_URL = "redis://127.0.0.1:1/0"
    with caplog.at_level("WARNING", logger="common.health"):
        r = client.get("/health/redis/")
    assert r.status_code == 503
    assert r.json() == {"status": "error", "component": "redis"}
    assert "health/redis: ConnectionError" in caplog.text
    assert "127.0.0.1" in caplog.text
