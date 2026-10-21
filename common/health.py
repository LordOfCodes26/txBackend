import json
import logging
import os
import shutil
from datetime import UTC, datetime, timedelta

import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


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
            settings.REDIS_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
            **getattr(settings, "REDIS_CONNECTION_KWARGS", {}),
        )
        client.ping()
    except Exception as exc:
        # The reason goes to the log only (the endpoint is public).
        logger.warning(
            "health/redis: %s: %s (REDIS_URL host %s)",
            type(exc).__name__,
            exc,
            redis.connection.parse_url(settings.REDIS_URL).get("host"),
        )
        return JsonResponse({"status": "error", "component": "redis"}, status=503)
    return JsonResponse({"status": "ok", "component": "redis"})


def _parse(ts: str | None):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


# Why the last off-site copy didn't happen (written by scripts/backup/common.sh).
OFFSITE_ERRORS = {
    "NOT_MOUNTED": "off-site copy refused: the backup disk isn't mounted",
    "COPY_FAILED": "off-site copy failed",
}


def _existing(path: str) -> str:
    """The path, or its nearest existing parent (a folder not made yet is on its parent's disk)."""
    while path and not os.path.exists(path):
        parent = os.path.dirname(path.rstrip("/")) or "/"
        if parent == path:
            break
        path = parent
    return path or "/"


def disk_usage(status: dict) -> list[dict]:
    """How full the backup disk is, and a local off-site disk (once per filesystem). Only
    the role is named, not the folder (the health endpoint is public)."""
    places = [("backups", status.get("backup_dir") or settings.BACKUP_DIR)]
    target = status.get("offsite_target") or ""
    if target.startswith("/") and not status.get("offsite_error"):
        places.append(("off-site", target))
    disks, seen = [], set()
    for role, path in places:
        try:
            path = _existing(path)
            device = os.stat(path).st_dev
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if device in seen:
            continue
        seen.add(device)
        # As `df` counts it: what is used of what an ordinary user can have.
        usable = usage.used + usage.free
        percent = round(100 * usage.used / usable, 1) if usable else 0.0
        disks.append({"role": role, "used_percent": percent, "free_bytes": usage.free})
    return disks


def backup_health() -> dict:
    """Backup freshness: `status` "error" when backups are missing, stale, unverified, the
    off-site copy failed or is old, a backup disk is 90% full or WAL archiving fails, with
    `errors`; `warnings` (e.g. no off-site copy, a disk 80% full) otherwise. With
    BACKUP_REQUIRE_PITR off, base backups and WAL archiving aren't required."""
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
    if (
        settings.BACKUP_REQUIRE_PITR
        and status
        and (base_at is None or now - base_at > timedelta(days=settings.BACKUP_MAX_BASE_AGE_DAYS))
    ):
        errors.append("base backup for point-in-time recovery is missing or too old")
    if status and not status.get("offsite_target"):
        warnings.append("backups are only on this server's disk (no off-site copy configured)")
    elif status.get("offsite_error"):
        errors.append(OFFSITE_ERRORS.get(status["offsite_error"], "off-site copy failed"))
    elif status:
        copied_at = _parse(status.get("offsite_copy_at"))
        if copied_at is None or now - copied_at > timedelta(
            hours=settings.BACKUP_MAX_DUMP_AGE_HOURS
        ):
            errors.append("off-site copy is missing or too old")

    pruned_at = _parse(status.get("space_pruned_at"))
    if pruned_at is not None and now - pruned_at < timedelta(days=7):
        warnings.append(
            "the backup disk was nearly full: the oldest backups were deleted to make room"
        )

    disks = disk_usage(status)
    for disk in disks:
        if disk["used_percent"] >= settings.BACKUP_DISK_ERROR_PERCENT:
            errors.append(f"{disk['role']} disk is {disk['used_percent']:g}% full")
        elif disk["used_percent"] >= settings.BACKUP_DISK_WARN_PERCENT:
            warnings.append(f"{disk['role']} disk is {disk['used_percent']:g}% full")

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
        if not settings.BACKUP_REQUIRE_PITR:
            pass  # nightly dumps only: archiving is reported, not required
        elif archive_mode != "on":
            errors.append("WAL archiving is off (no point-in-time recovery)")
        elif last_fail and (not last_ok or last_fail > last_ok):
            errors.append("WAL archiving is failing")
    except Exception:
        if settings.BACKUP_REQUIRE_PITR:
            errors.append("could not read archiver status")

    body = {
        "status": "error" if errors else "ok",
        "errors": errors,
        "warnings": warnings,
        "last_dump_at": status.get("last_dump_at"),
        "last_verify_ok": status.get("last_verify_ok") == "true",
        "last_base_backup_at": status.get("last_base_backup_at"),
        "offsite_copy_at": status.get("offsite_copy_at"),
        "offsite_error": status.get("offsite_error") or None,
        "disks": disks,
        "wal_archiver": archiver,
    }
    return body


@require_GET
def health_backup(request):
    """503 when the backups need attention (see `backup_health`), else 200."""
    body = backup_health()
    return JsonResponse(body, status=503 if body["errors"] else 200)
