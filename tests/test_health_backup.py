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
def test_reports_wal_archiver_state(client, status_file):
    status_file(**healthy())
    body = client.get("/health/backup/").json()
    assert "archive_mode" in body["wal_archiver"]
    # The test database server may or may not archive; either way it is reported.
    if body["wal_archiver"]["archive_mode"] != "on":
        assert "WAL archiving is off (no point-in-time recovery)" in body["errors"]


@pytest.mark.django_db
def test_without_pitr_only_nightly_dumps_are_required(client, status_file, settings):
    # The Windows kit: verified nightly dumps, no base backups or WAL archiving.
    settings.BACKUP_REQUIRE_PITR = False
    status_file(**healthy(last_base_backup_at=""))
    r = client.get("/health/backup/")
    assert r.status_code == 200, r.json()
    assert r.json()["errors"] == []


def test_redis_failure_is_logged_with_its_reason(client, settings, caplog):
    # The endpoint only says "error"; the log says why (it took two rounds on Windows).
    settings.REDIS_URL = "redis://127.0.0.1:1/0"
    with caplog.at_level("WARNING", logger="common.health"):
        r = client.get("/health/redis/")
    assert r.status_code == 503
    assert r.json() == {"status": "error", "component": "redis"}
    assert "health/redis: ConnectionError" in caplog.text
    assert "127.0.0.1" in caplog.text
