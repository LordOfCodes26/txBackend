"""Delete a record for good, with its history (Admin, `system.delete_records`).

Every delete makes a database backup first and then runs in one transaction. What goes
with each record:

- developer: their scans, attendance, card assignments (the cards stay, unassigned),
  account and money transactions, purchases and bookings. Their login stays.
- card: its scans and the attendance made from them (those days are recomputed), its
  assignments. Purchases paid with it stay (sales and money are kept), without the card.
- reader: its scans and the attendance made from them (recomputed), its TCP log. Purchases
  through it stay, without the reader.
- seller / service position / good: the goods, their stock history and the purchases and
  bookings there (for a good: the purchases that include it). The developers' money
  transactions for those purchases stay, so balances don't change. Till readers of a
  deleted seller stay, unassigned.
- user: the login and its roles. What they did stays (audit log, transactions, ...) under
  their username; their developer or seller profile stays, unlinked. Not yourself, not the
  last active admin.

The ledgers and logs are append-only (database triggers); their triggers are switched off
inside the transaction for exactly these rows and on again before it commits.
"""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from django.db import connection, transaction
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role, User
from apps.attendance.models import AttendanceRecord, DailyAttendance
from apps.attendance.occupancy import refresh_presence
from apps.attendance.services import recompute_day
from apps.audit.models import AuditLog
from apps.audit.services import record_audit
from apps.bookings.models import Booking
from apps.developers.models import Developer
from apps.finance.models import AccountTransaction, DeveloperAccount
from apps.goods.models import Good, GoodImage, InventoryMovement
from apps.purchases.models import Purchase
from apps.realtime.notify import notify_occupancy
from apps.rfid.models import RFIDCard, RFIDCardAssignment, RFIDDevice, RFIDEvent, TCPFrameLog
from apps.sellers.models import Seller, ServicePosition
from common import data_reset
from common.exceptions import DomainError

CONFIRM_PHRASE = "DELETE"


class DeleteNotConfirmed(DomainError):
    code = "DELETE_NOT_CONFIRMED"
    default_detail = _("Type DELETE to confirm.")


class CannotDelete(DomainError):
    status_code = 409
    code = "CANNOT_DELETE"


@contextmanager
def _writable(*models):
    """Let these append-only tables be changed inside the current transaction."""
    tables = [m._meta.db_table for m in models]
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        # Pending deferred FK checks would block ALTER TABLE: settle them first.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        for table in tables:
            cursor.execute(f"ALTER TABLE {qn(table)} DISABLE TRIGGER {qn(table + '_append_only')}")
    yield
    # Not in `finally`: after an error the rollback restores the triggers anyway.
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f"ALTER TABLE {qn(table)} ENABLE TRIGGER {qn(table + '_append_only')}")


@dataclass
class Plan:
    """What deleting one record does: counts for the preview, and the deletion itself."""

    label: str
    deletes: dict[str, int]
    keeps: dict[str, int]
    run: Callable[[], None]


def _recompute(records: QuerySet) -> Callable[[], None]:
    """Delete attendance records, then recompute the days and presence they were part of."""
    days = set(records.values_list("developer_id", "work_date"))

    def after():
        for developer_id, work_date in days:
            recompute_day(developer_id, work_date)
        for developer_id in {d for d, _day in days}:
            refresh_presence(developer_id)
        notify_occupancy()

    return after


def _purchases_gone(purchases: QuerySet) -> None:
    """Bookings and purchases (items go with them). Money transactions stay."""
    Booking.objects.filter(purchase__in=purchases).delete()
    purchases.delete()


def _goods_gone(goods: QuerySet) -> None:
    files = [image.image for image in GoodImage.objects.filter(good__in=goods)]
    with _writable(InventoryMovement):
        InventoryMovement.objects.filter(good__in=goods).delete()
    Booking.objects.filter(good__in=goods).delete()
    goods.delete()
    transaction.on_commit(lambda: [f.delete(save=False) for f in files])


def plan_developer(developer: Developer) -> Plan:
    purchases = Purchase.objects.filter(developer=developer)
    bookings = Booking.objects.filter(Q(developer=developer) | Q(purchase__in=purchases))
    scans = RFIDEvent.objects.filter(developer=developer)
    records = AttendanceRecord.objects.filter(developer=developer)
    transactions = AccountTransaction.objects.filter(account__developer=developer)

    def run():
        bookings.delete()
        purchases.delete()
        # Someone else's purchase that points at one of these taps keeps the sale.
        Purchase.objects.filter(presented_event__in=scans).update(presented_event=None)
        with _writable(AccountTransaction, RFIDEvent):
            transactions.delete()
            DeveloperAccount.objects.filter(developer=developer).delete()
            records.delete()
            DailyAttendance.objects.filter(developer=developer).delete()
            scans.delete()
        RFIDCardAssignment.objects.filter(developer=developer).delete()
        Developer.all_objects.filter(pk=developer.pk).delete()
        notify_occupancy()

    return Plan(
        label=f"{developer.employee_number} {developer.full_name}",
        deletes={
            "scans": scans.count(),
            "attendance_records": records.count(),
            "attendance_days": DailyAttendance.objects.filter(developer=developer).count(),
            "card_assignments": RFIDCardAssignment.objects.filter(developer=developer).count(),
            "transactions": transactions.count(),
            "purchases": purchases.count(),
            "bookings": bookings.count(),
        },
        keeps={"login": int(developer.user_id is not None)},
        run=run,
    )


def plan_card(card: RFIDCard) -> Plan:
    scans = RFIDEvent.objects.filter(card=card)
    records = AttendanceRecord.objects.filter(rfid_event__in=scans)
    purchases = Purchase.objects.filter(Q(card=card) | Q(presented_event__in=scans))

    def run():
        after = _recompute(records)
        purchases.update(card=None, presented_event=None)
        records.delete()
        with _writable(RFIDEvent):
            scans.delete()
        RFIDCardAssignment.objects.filter(card=card).delete()
        card.delete()
        after()

    return Plan(
        label=card.label or card.uid,
        deletes={
            "scans": scans.count(),
            "attendance_records": records.count(),
            "card_assignments": RFIDCardAssignment.objects.filter(card=card).count(),
        },
        keeps={"purchases": purchases.count()},
        run=run,
    )


def plan_reader(device: RFIDDevice) -> Plan:
    scans = RFIDEvent.objects.filter(device=device)
    records = AttendanceRecord.objects.filter(Q(device=device) | Q(rfid_event__in=scans))
    purchases = Purchase.objects.filter(Q(reader=device) | Q(presented_event__in=scans))
    log = TCPFrameLog.objects.filter(device=device)

    def run():
        after = _recompute(records)
        purchases.update(reader=None, presented_event=None)
        records.delete()
        log.delete()
        with _writable(RFIDEvent):
            scans.delete()
        device.delete()
        after()

    return Plan(
        label=f"{device.code} {device.name}".strip(),
        deletes={
            "scans": scans.count(),
            "attendance_records": records.count(),
            "tcp_log": log.count(),
        },
        keeps={"purchases": purchases.count()},
        run=run,
    )


def _store_plan(label, positions, goods, purchases, extra_run=None, keeps=None) -> Plan:
    bookings = Booking.objects.filter(Q(purchase__in=purchases) | Q(good__in=goods))

    def run():
        _purchases_gone(purchases)
        _goods_gone(goods)
        if positions is not None:
            positions.delete()
        if extra_run:
            extra_run()

    deletes = {
        "goods": goods.count(),
        "stock_movements": InventoryMovement.objects.filter(good__in=goods).count(),
        "purchases": purchases.count(),
        "bookings": bookings.count(),
    }
    if positions is not None:
        deletes = {"service_positions": positions.count(), **deletes}
    return Plan(label=label, deletes=deletes, keeps=keeps or {}, run=run)


def plan_seller(seller: Seller) -> Plan:
    positions = ServicePosition.all_objects.filter(seller=seller)
    goods = Good.all_objects.filter(service_position__seller=seller)
    purchases = Purchase.objects.filter(seller=seller)
    readers = RFIDDevice.objects.filter(seller=seller)

    def rest():
        readers.update(seller=None)
        Seller.objects.filter(pk=seller.pk).delete()

    return _store_plan(
        seller.name,
        positions,
        goods,
        purchases,
        rest,
        keeps={
            "money_transactions": purchases.exclude(account_transaction=None).count(),
            "readers": readers.count(),
        },
    )


def plan_position(position: ServicePosition) -> Plan:
    goods = Good.all_objects.filter(service_position=position)
    purchases = Purchase.objects.filter(service_position=position)
    return _store_plan(
        f"{position.seller.name} · {position.name}",
        ServicePosition.all_objects.filter(pk=position.pk),
        goods,
        purchases,
        keeps={"money_transactions": purchases.exclude(account_transaction=None).count()},
    )


def plan_good(good: Good) -> Plan:
    purchases = Purchase.objects.filter(items__good=good).distinct()
    return _store_plan(
        good.name,
        None,
        Good.all_objects.filter(pk=good.pk),
        Purchase.objects.filter(pk__in=purchases.values("pk")),
        keeps={"money_transactions": purchases.exclude(account_transaction=None).count()},
    )


def plan_user(user: User, actor) -> Plan:
    if user.pk == actor.pk:
        raise CannotDelete(_("You can't delete your own account."))
    admins = User.objects.filter(is_active=True, user_roles__role__code="ADMIN").distinct()
    if user in admins and admins.count() <= 1:
        raise CannotDelete(_("This is the last active admin: make another admin first."))
    actions = (
        AuditLog.objects.filter(actor=user).count()
        + AccountTransaction.objects.filter(actor=user).count()
        + InventoryMovement.objects.filter(actor=user).count()
    )

    def run():
        Developer.all_objects.filter(user=user).update(user=None)
        Seller.objects.filter(user=user).update(user=None)
        ServicePosition.all_objects.filter(manager=user).update(manager=None)
        Purchase.objects.filter(created_by=user).update(created_by=None)
        Purchase.objects.filter(confirmed_by=user).update(confirmed_by=None)
        RFIDCardAssignment.objects.filter(assigned_by=user).update(assigned_by=None)
        RFIDCardAssignment.objects.filter(unassigned_by=user).update(unassigned_by=None)
        AttendanceRecord.objects.filter(created_by=user).update(created_by=None)
        AttendanceRecord.objects.filter(voided_by=user).update(voided_by=None)
        with _writable(AuditLog, AccountTransaction, InventoryMovement):
            # The audit log keeps the name in actor_username.
            AuditLog.objects.filter(actor=user).update(actor=None)
            AccountTransaction.objects.filter(actor=user).update(actor=None)
            InventoryMovement.objects.filter(actor=user).update(actor=None)
        User.objects.filter(pk=user.pk).delete()

    return Plan(
        label=user.username,
        deletes={"roles": Role.objects.filter(user_roles__user=user).count()},
        keeps={
            "actions": actions,
            "developer_profile": Developer.all_objects.filter(user=user).count(),
            "seller_profile": Seller.objects.filter(user=user).count(),
        },
        run=run,
    )


KINDS = {
    "user": (lambda pk: User.objects.get(pk=pk), plan_user),
    "developer": (lambda pk: Developer.all_objects.get(pk=pk), plan_developer),
    "card": (lambda pk: RFIDCard.objects.get(pk=pk), plan_card),
    "reader": (lambda pk: RFIDDevice.objects.get(pk=pk), plan_reader),
    "seller": (lambda pk: Seller.objects.get(pk=pk), plan_seller),
    "position": (lambda pk: ServicePosition.all_objects.get(pk=pk), plan_position),
    "good": (lambda pk: Good.all_objects.get(pk=pk), plan_good),
}


def plan(kind: str, pk: int, actor) -> Plan:
    load, build = KINDS[kind]
    record = load(pk)
    return build(record, actor) if kind == "user" else build(record)


def delete_record(kind: str, pk: int, *, actor, confirm: str) -> dict:
    if confirm != CONFIRM_PHRASE:
        raise DeleteNotConfirmed()
    preview = plan(kind, pk, actor)
    backup = data_reset.backup_database(prefix=f"before-delete-{kind}")
    with transaction.atomic():
        current = plan(kind, pk, actor)  # fresh querysets inside the transaction
        current.run()
        record_audit(
            f"system.{kind}_deleted",
            actor=actor,
            entity_type=kind,
            entity_id=pk,
            old_values={"record": preview.label},
            new_values={"deleted": current.deletes, "kept": current.keeps, "backup": str(backup)},
        )
    return {"label": preview.label, "deleted": current.deletes, "backup": str(backup)}
