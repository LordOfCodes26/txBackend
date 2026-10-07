"""Delete all business data, keeping the setup: users, roles, readers and the stores.

Deleted: developers, RFID cards and their assignments, every scan, the TCP log,
attendance, developer accounts and ledgers, purchases, bookings, stock history and the
audit log.

Kept: users and roles, RFID readers (door units, tills, card assign readers), buildings,
sellers, counters and goods. Each tracked good keeps its current stock as one opening
movement, so the stock ledger still adds up. The audit log starts again with one entry
saying who reset the data, what was deleted and where the backup is.

A database backup (pg_dump) is made first; without it nothing is deleted.
"""

import os
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role, User
from apps.attendance.models import AttendanceRecord, DailyAttendance, DeveloperPresence
from apps.audit.models import AuditLog
from apps.audit.services import record_audit
from apps.bookings.models import Booking
from apps.developers.models import Developer
from apps.finance.models import AccountTransaction, DeveloperAccount
from apps.goods.models import Good, InventoryMovement, MovementKind
from apps.purchases.models import Purchase, PurchaseItem
from apps.realtime.notify import notify_occupancy
from apps.rfid.models import (
    Building,
    RFIDCard,
    RFIDCardAssignment,
    RFIDDevice,
    RFIDEvent,
    TCPFrameLog,
)
from apps.sellers.models import Seller, ServicePosition
from common.exceptions import DomainError

# What the admin types to confirm (the same in every language).
CONFIRM_PHRASE = "DELETE ALL DATA"

DELETED = {
    "developers": Developer,
    "cards": RFIDCard,
    "card_assignments": RFIDCardAssignment,
    "scans": RFIDEvent,
    "tcp_log": TCPFrameLog,
    "attendance_records": AttendanceRecord,
    "daily_attendance": DailyAttendance,
    "presence": DeveloperPresence,
    "accounts": DeveloperAccount,
    "transactions": AccountTransaction,
    "purchases": Purchase,
    "purchase_items": PurchaseItem,
    "bookings": Booking,
    "stock_movements": InventoryMovement,
    "audit_log": AuditLog,
}
KEPT = {
    "users": User,
    "roles": Role,
    "readers": RFIDDevice,
    "buildings": Building,
    "sellers": Seller,
    "counters": ServicePosition,
    "goods": Good,
}


class ResetNotConfirmed(DomainError):
    code = "RESET_NOT_CONFIRMED"
    default_detail = _("Type the confirmation phrase exactly to delete all data.")


class BackupFailed(DomainError):
    status_code = 500
    code = "BACKUP_FAILED"
    default_detail = _("The backup before the reset failed, so nothing was deleted.")


def _counts(models: dict) -> dict[str, int]:
    # _base_manager: soft-deleted developers and goods count too.
    return {key: model._base_manager.count() for key, model in models.items()}


def summary() -> dict:
    """What a reset would delete and keep, with the row counts."""
    return {"phrase": CONFIRM_PHRASE, "delete": _counts(DELETED), "keep": _counts(KEPT)}


def backup_dir() -> Path:
    """DATA_RESET_BACKUP_DIR, else `reset-backups` next to the media folder
    (/var/lib/backend/reset-backups: a folder the backend may write to)."""
    configured = getattr(settings, "DATA_RESET_BACKUP_DIR", "")
    return Path(configured) if configured else Path(settings.MEDIA_ROOT).parent / "reset-backups"


def _pg_dump() -> str:
    found = getattr(settings, "PG_DUMP", "") or shutil.which("pg_dump")
    if not found:
        raise BackupFailed(_("pg_dump was not found; set PG_DUMP to its path."))
    return found


def backup_database(prefix: str = "before-reset") -> Path:
    """A full pg_dump (custom format) of the database; returns the file."""
    db = connection.settings_dict
    folder = backup_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupFailed(details={"folder": str(folder), "reason": str(exc)}) from exc
    target = folder / f"{prefix}-{timezone.now():%Y%m%d-%H%M%S}.dump"
    partial = target.with_suffix(".dump.partial")
    env = {**os.environ, "PGPASSWORD": str(db.get("PASSWORD") or "")}
    command = [_pg_dump(), "--format=custom", "--compress=6", f"--file={partial}"]
    for flag, key in (("--host", "HOST"), ("--port", "PORT"), ("--username", "USER")):
        if db.get(key):
            command.append(f"{flag}={db[key]}")
    command.append(db["NAME"])
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        partial.unlink(missing_ok=True)
        raise BackupFailed(details={"reason": str(exc)}) from exc
    if result.returncode != 0 or not partial.exists() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        raise BackupFailed(details={"reason": (result.stderr or "").strip()[-500:]})
    partial.replace(target)
    return target


def reset_data(*, actor, confirm: str, backup: bool = True) -> dict:
    """Back up, then delete all business data (see the module docstring).

    Returns {"deleted": {key: rows}, "backup": file or None, "opening_stock": goods}.
    """
    if confirm != CONFIRM_PHRASE:
        raise ResetNotConfirmed()
    backup_file = backup_database() if backup else None
    with transaction.atomic():
        deleted = _counts(DELETED)
        # TRUNCATE: one statement for all tables (they reference each other), and it
        # bypasses the per-row append-only triggers on the ledgers and logs. No CASCADE:
        # if a kept table still pointed at one of these, it fails instead of spreading.
        tables = ", ".join(connection.ops.quote_name(m._meta.db_table) for m in DELETED.values())
        with connection.cursor() as cursor:
            # Django's foreign keys are checked at commit; settle checks still pending from
            # earlier writes in this transaction, which TRUNCATE would otherwise refuse.
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute(f"TRUNCATE {tables}")
        opening = 0
        for good in Good.all_objects.filter(track_stock=True, quantity__gt=0).order_by("pk"):
            InventoryMovement.objects.create(
                good=good,
                kind=MovementKind.INITIAL_STOCK,
                quantity_delta=good.quantity,
                quantity_after=good.quantity,
                reason="Stock at the data reset",
                actor=actor,
            )
            opening += 1
        record_audit(
            "system.data_reset",
            actor=actor,
            entity_type="system",
            entity_id="data",
            new_values={
                "deleted": deleted,
                "backup": str(backup_file) if backup_file else None,
                "opening_stock": opening,
            },
        )
        notify_occupancy()  # everyone is outside now
    return {
        "deleted": deleted,
        "backup": str(backup_file) if backup_file else None,
        "opening_stock": opening,
    }
