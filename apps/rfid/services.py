from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.attendance.services import record_from_scan
from apps.audit.services import diff, record_audit, snapshot
from apps.developers.models import Developer, DeveloperStatus

from .exceptions import (
    CardAlreadyAssigned,
    CardNotActive,
    CardNotAssigned,
    DeveloperAlreadyHasCard,
    DeveloperNotAssignable,
    InvalidCardTransition,
)
from .models import (
    AssignmentEndReason,
    CardStatus,
    DevicePurpose,
    RFIDCard,
    RFIDCardAssignment,
    RFIDDevice,
    RFIDEvent,
    ScanResult,
)

DEVICE_FIELDS = [
    "code",
    "name",
    "location",
    "purpose",
    "direction",
    "service_position",
    "is_active",
]
REJECTED_DEVELOPER_STATUSES = {DeveloperStatus.SUSPENDED, DeveloperStatus.TERMINATED}


def _active_assignment(card: RFIDCard) -> RFIDCardAssignment | None:
    return (
        RFIDCardAssignment.objects.select_related("developer")
        .filter(card=card, unassigned_at__isnull=True)
        .first()
    )


def _lock_cards(*cards: RFIDCard) -> list[RFIDCard]:
    """Lock in primary-key order so concurrent operations cannot deadlock."""
    ids = sorted(c.pk for c in cards)
    locked = {c.pk: c for c in RFIDCard.objects.select_for_update().filter(pk__in=ids)}
    return [locked[c.pk] for c in cards]


# --- Cards -------------------------------------------------------------------


@transaction.atomic
def register_card(*, actor, uid: str, label: str = "", notes: str = "") -> RFIDCard:
    card = RFIDCard.objects.create(uid=uid, label=label, notes=notes)
    record_audit(
        "rfid.card_registered", actor=actor, entity=card, new_values={"uid": uid, "label": label}
    )
    return card


def _assign(*, actor, card: RFIDCard, developer: Developer) -> RFIDCardAssignment:
    if card.status != CardStatus.ACTIVE:
        raise CardNotActive()
    developer = Developer.all_objects.select_for_update().get(pk=developer.pk)
    if developer.deleted_at or developer.status == DeveloperStatus.TERMINATED:
        raise DeveloperNotAssignable()
    if _active_assignment(card):
        raise CardAlreadyAssigned()
    if RFIDCardAssignment.objects.filter(developer=developer, unassigned_at__isnull=True).exists():
        raise DeveloperAlreadyHasCard()
    return RFIDCardAssignment.objects.create(card=card, developer=developer, assigned_by=actor)


def _end(assignment: RFIDCardAssignment, *, actor, reason: str) -> None:
    assignment.unassigned_at = timezone.now()
    assignment.unassigned_by = actor
    assignment.end_reason = reason
    assignment.save(update_fields=["unassigned_at", "unassigned_by", "end_reason"])


@transaction.atomic
def assign_card(*, actor, card: RFIDCard, developer: Developer) -> RFIDCardAssignment:
    (card,) = _lock_cards(card)
    assignment = _assign(actor=actor, card=card, developer=developer)
    record_audit(
        "rfid.card_assigned",
        actor=actor,
        entity=card,
        new_values={"developer": developer.pk, "assignment": assignment.pk},
    )
    return assignment


@transaction.atomic
def unassign_card(
    *, actor, card: RFIDCard, reason: str = AssignmentEndReason.RETURNED
) -> RFIDCardAssignment:
    (card,) = _lock_cards(card)
    assignment = _active_assignment(card)
    if assignment is None:
        raise CardNotAssigned()
    _end(assignment, actor=actor, reason=reason)
    record_audit(
        "rfid.card_unassigned",
        actor=actor,
        entity=card,
        old_values={"developer": assignment.developer_id},
        new_values={"reason": reason},
    )
    return assignment


@transaction.atomic
def replace_card(
    *, actor, old_card: RFIDCard, new_card: RFIDCard, reason: str = ""
) -> RFIDCardAssignment:
    """Move the developer from `old_card` to `new_card` and retire `old_card`."""
    old_card, new_card = _lock_cards(old_card, new_card)
    old_assignment = _active_assignment(old_card)
    if old_assignment is None:
        raise CardNotAssigned()
    developer = old_assignment.developer

    _end(old_assignment, actor=actor, reason=AssignmentEndReason.REPLACED)
    new_assignment = _assign(actor=actor, card=new_card, developer=developer)
    old_card.status = CardStatus.RETIRED
    old_card.status_reason = reason or "Replaced"
    old_card.save(update_fields=["status", "status_reason", "updated_at"])

    record_audit(
        "rfid.card_replaced",
        actor=actor,
        entity=developer,
        old_values={"card": old_card.pk, "uid": old_card.uid},
        new_values={"card": new_card.pk, "uid": new_card.uid, "reason": reason},
    )
    return new_assignment


_TRANSITIONS = {
    "block": ({CardStatus.ACTIVE}, CardStatus.BLOCKED, "rfid.card_blocked"),
    "unblock": ({CardStatus.BLOCKED}, CardStatus.ACTIVE, "rfid.card_unblocked"),
    "retire": ({CardStatus.ACTIVE, CardStatus.BLOCKED}, CardStatus.RETIRED, "rfid.card_retired"),
}


@transaction.atomic
def change_card_status(*, actor, card: RFIDCard, transition: str, reason: str = "") -> RFIDCard:
    """Blocking keeps the owner (scans are rejected); retiring requires no owner."""
    allowed_from, target, action = _TRANSITIONS[transition]
    (card,) = _lock_cards(card)
    if card.status not in allowed_from:
        raise InvalidCardTransition()
    if target == CardStatus.RETIRED and _active_assignment(card):
        raise InvalidCardTransition("Unassign or replace the card before retiring it.")
    old_status = card.status
    card.status = target
    card.status_reason = reason
    card.save(update_fields=["status", "status_reason", "updated_at"])
    record_audit(
        action,
        actor=actor,
        entity=card,
        old_values={"status": old_status},
        new_values={"status": target, "reason": reason},
    )
    return card


def end_developer_assignment(*, actor, developer: Developer, reason: str) -> None:
    """Called when a developer is terminated or deleted. Must run inside a transaction."""
    assignment = (
        RFIDCardAssignment.objects.select_for_update()
        .filter(developer=developer, unassigned_at__isnull=True)
        .first()
    )
    if assignment is None:
        return
    _end(assignment, actor=actor, reason=reason)
    record_audit(
        "rfid.card_unassigned",
        actor=actor,
        entity=assignment.card,
        old_values={"developer": developer.pk},
        new_values={"reason": reason},
    )


# --- Devices -----------------------------------------------------------------


@transaction.atomic
def register_device(*, actor, **data) -> tuple[RFIDDevice, str]:
    device = RFIDDevice(**data)
    key = device.set_new_api_key()
    device.save()
    record_audit(
        "rfid.device_registered",
        actor=actor,
        entity=device,
        new_values=snapshot(device, DEVICE_FIELDS),
    )
    return device, key


@transaction.atomic
def rotate_device_key(*, actor, device: RFIDDevice) -> str:
    device = RFIDDevice.objects.select_for_update().get(pk=device.pk)
    key = device.set_new_api_key()
    device.save(update_fields=["api_key_prefix", "api_key_hash", "updated_at"])
    record_audit("rfid.device_key_rotated", actor=actor, entity=device)
    return key


@transaction.atomic
def update_device(*, actor, device: RFIDDevice, **changes) -> RFIDDevice:
    device = RFIDDevice.objects.select_for_update().get(pk=device.pk)
    before = snapshot(device, DEVICE_FIELDS)
    for field, value in changes.items():
        setattr(device, field, value)
    device.save()
    old, new = diff(before, snapshot(device, DEVICE_FIELDS))
    if new:
        record_audit(
            "rfid.device_updated", actor=actor, entity=device, old_values=old, new_values=new
        )
    return device


# --- Scans -------------------------------------------------------------------


def record_scan(
    *,
    device: RFIDDevice,
    uid: str,
    event_time=None,
    client_event_id: str = "",
    direction: str = "",
) -> tuple[RFIDEvent, bool]:
    """Store and classify one scan. Returns (event, created).

    A retry carrying an already-seen `client_event_id` returns the original event.
    Scans of the same card are serialized by a row lock, so two readers hit at the same
    moment cannot both produce an ACCEPTED event inside the debounce window.
    """
    now = timezone.now()
    if client_event_id:
        existing = RFIDEvent.objects.filter(device=device, client_event_id=client_event_id).first()
        if existing:
            return existing, False
    try:
        with transaction.atomic():
            event = _classify_and_store(
                device=device,
                uid=uid,
                event_time=event_time or now,
                received_at=now,
                client_event_id=client_event_id,
                direction=direction,
            )
    except IntegrityError:
        if not client_event_id:
            raise
        # A concurrent retry of the same scan won the insert.
        return RFIDEvent.objects.get(device=device, client_event_id=client_event_id), False

    RFIDDevice.objects.filter(pk=device.pk).filter(
        Q(last_seen_at__isnull=True) | Q(last_seen_at__lt=now - timedelta(seconds=60))
    ).update(last_seen_at=now)
    if device.purpose == DevicePurpose.TILL and event.result == ScanResult.ACCEPTED:
        # Separate transaction, after the card lock is released: checkout locks the
        # purchase before the card, so locking them in the other order here could deadlock.
        from apps.purchases.services import present_card

        present_card(event)
    if device.purpose == DevicePurpose.TILL:
        from apps.realtime.notify import notify_card_tapped

        notify_card_tapped(event)
    return event, True


def _classify_and_store(
    *, device, uid, event_time, received_at, client_event_id, direction=""
) -> RFIDEvent:
    card = RFIDCard.objects.select_for_update().filter(uid=uid).first()
    assignment = _active_assignment(card) if card else None
    developer = assignment.developer if assignment else None

    if card is None:
        result = ScanResult.UNKNOWN_CARD
    elif card.status == CardStatus.BLOCKED:
        result = ScanResult.BLOCKED_CARD
    elif card.status == CardStatus.RETIRED:
        result = ScanResult.RETIRED_CARD
    elif developer is None:
        result = ScanResult.UNASSIGNED_CARD
    elif developer.deleted_at or developer.status in REJECTED_DEVELOPER_STATUSES:
        result = ScanResult.INACTIVE_DEVELOPER
    elif device.purpose == DevicePurpose.ATTENDANCE and _is_duplicate(card, event_time, direction):
        # Till taps are never debounced: paying twice in a row is legitimate.
        result = ScanResult.DUPLICATE
    else:
        result = ScanResult.ACCEPTED

    event = RFIDEvent.objects.create(
        device=device,
        client_event_id=client_event_id,
        uid=uid,
        card=card,
        developer=developer,
        event_time=event_time,
        received_at=received_at,
        direction=direction if device.purpose == DevicePurpose.ATTENDANCE else "",
        result=result,
    )
    record_from_scan(event)
    return event


def _is_duplicate(card: RFIDCard, event_time, direction: str = "") -> bool:
    # Symmetric window: buffered scans can arrive out of order. A scan reporting the
    # opposite direction (in, then out) is a real movement, not a repeat.
    window = timedelta(seconds=settings.RFID_DEBOUNCE_SECONDS)
    return RFIDEvent.objects.filter(
        card=card,
        result=ScanResult.ACCEPTED,
        direction=direction,
        event_time__gt=event_time - window,
        event_time__lt=event_time + window,
    ).exists()


def heartbeat(*, device: RFIDDevice, ip_address: str | None, app_version: str = "") -> RFIDDevice:
    """Mark a device as alive. Devices call this every RFID_HEARTBEAT_SECONDS."""
    fields = {"last_seen_at": timezone.now(), "last_ip": ip_address}
    if app_version:
        fields["app_version"] = app_version
    RFIDDevice.objects.filter(pk=device.pk).update(**fields)
    device.refresh_from_db()
    return device


def record_batch(*, device: RFIDDevice, events: list[dict]) -> list[tuple[RFIDEvent, bool]]:
    """Upload scans an attendance reader buffered while offline, oldest first.

    Each scan goes through `record_scan`, so retries of a partly uploaded batch are safe
    (every item carries a client_event_id) and debouncing sees the scans in time order.
    """
    ordered = sorted(events, key=lambda e: e["event_time"])
    return [
        record_scan(
            device=device,
            uid=item["uid"],
            event_time=item["event_time"],
            client_event_id=item["client_event_id"],
            direction=item.get("direction", ""),
        )
        for item in ordered
    ]
