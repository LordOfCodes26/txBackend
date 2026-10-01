import json
from datetime import UTC, datetime, timedelta

import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})


@require_GET
def health_db(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "error", "component": "db"}, status=503)
    return JsonResponse({"status": "ok", "component": "db"})


@require_GET
def health_redis(request):
    try:
        client = redis.Redis.from_url(
            settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2
        )
        client.ping()
    except Exception:
        return JsonResponse({"status": "error", "component": "redis"}, status=503)
    return JsonResponse({"status": "ok", "component": "redis"})


def _parse(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


@require_GET
def health_backup(request):
    """Backup freshness: 503 when backups are missing, stale, unverified or WAL archiving
    fails; 200 with `warnings` (e.g. no off-site copy) otherwise."""
    errors, warnings = [], []
    try:
        with open(settings.BACKUP_STATUS_FILE) as f:
            status = json.load(f)
    except (OSError, ValueError):
        status = {}
        errors.append("no backup status yet (has the nightly backup ever run?)")

    now = datetime.now(UTC)
    dump_at = _parse(status.get("last_dump_at"))
    if status and (
        dump_at is None or now - dump_at > timedelta(hours=settings.BACKUP_MAX_DUMP_AGE_HOURS)
    ):
        errors.append("last database dump is missing or too old")
    if status and status.get("last_verify_ok") != "true":
        errors.append("last dump failed its restore check")
    base_at = _parse(status.get("last_base_backup_at"))
    if status and (
        base_at is None or now - base_at > timedelta(days=settings.BACKUP_MAX_BASE_AGE_DAYS)
    ):
        errors.append("base backup for point-in-time recovery is missing or too old")
    if status and not status.get("offsite_target"):
        warnings.append("backups are only on this server's disk (no off-site copy configured)")

    archiver = {}
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW archive_mode")
            archive_mode = cursor.fetchone()[0]
            cursor.execute(
                "SELECT archived_count, failed_count, last_archived_time, last_failed_time "
                "FROM pg_stat_archiver"
            )
            archived, failed, last_ok, last_fail = cursor.fetchone()
        archiver = {
            "archive_mode": archive_mode,
            "archived_count": archived,
            "failed_count": failed,
            "last_archived_at": last_ok.isoformat() if last_ok else None,
        }
        if archive_mode != "on":
            errors.append("WAL archiving is off (no point-in-time recovery)")
        elif last_fail and (not last_ok or last_fail > last_ok):
            errors.append("WAL archiving is failing")
    except Exception:
        errors.append("could not read archiver status")

    body = {
        "status": "error" if errors else "ok",
        "errors": errors,
        "warnings": warnings,
        "last_dump_at": status.get("last_dump_at"),
        "last_verify_ok": status.get("last_verify_ok") == "true",
        "last_base_backup_at": status.get("last_base_backup_at"),
        "offsite_copy_at": status.get("offsite_copy_at"),
        "wal_archiver": archiver,
    }
    return JsonResponse(body, status=503 if errors else 200)
