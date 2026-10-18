from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.audit.services import diff, record_audit, snapshot
from apps.finance.services import open_account
from apps.rfid.models import AssignmentEndReason
from apps.rfid.services import end_developer_assignment

from .models import Developer, DeveloperStatus

AUDITED_FIELDS = [
    "user",
    "building",
    "employee_number",
    "full_name",
    "phone",
    "home_address",
    "birthday",
    "department",
    "position_title",
    "start_date",
    "out_date",
    "status",
]


@transaction.atomic
def create_developer(*, actor, **data) -> Developer:
    developer = Developer.objects.create(**data)
    open_account(developer)
    record_audit(
        "developer.created",
        actor=actor,
        entity=developer,
        new_values=snapshot(developer, AUDITED_FIELDS),
    )
    return developer


@transaction.atomic
def update_developer(*, actor, developer: Developer, **changes) -> Developer:
    developer = Developer.objects.select_for_update().get(pk=developer.pk)
    before = snapshot(developer, AUDITED_FIELDS)
    # Becoming Terminated without a last working day: it is today.
    leaving = (
        changes.get("status") == DeveloperStatus.TERMINATED
        and developer.status != DeveloperStatus.TERMINATED
    )
    if leaving:
        # Only with an empty wallet (pay out or adjust it first).
        from apps.finance.services import ensure_wallet_empty

        ensure_wallet_empty(developer)
    if leaving and not changes.get("out_date") and not developer.out_date:
        changes["out_date"] = timezone.localdate()
    for field, value in changes.items():
        setattr(developer, field, value)
    developer.save()
    old, new = diff(before, snapshot(developer, AUDITED_FIELDS))
    if new:
        record_audit(
            "developer.updated", actor=actor, entity=developer, old_values=old, new_values=new
        )
    if new.get("status") == DeveloperStatus.TERMINATED:
        end_developer_assignment(
            actor=actor, developer=developer, reason=AssignmentEndReason.DEVELOPER_LEFT
        )
        # Leaving (the wallet is at 0): it is closed.
        from apps.finance.services import close_on_leaving

        close_on_leaving(actor=actor, developer=developer)
    return developer


@transaction.atomic
def delete_developer(*, actor, developer: Developer) -> None:
    developer = Developer.objects.select_for_update().get(pk=developer.pk)
    developer.deleted_at = timezone.now()
    developer.save(update_fields=["deleted_at", "updated_at"])
    end_developer_assignment(
        actor=actor, developer=developer, reason=AssignmentEndReason.DEVELOPER_DELETED
    )
    record_audit(
        "developer.deleted",
        actor=actor,
        entity=developer,
        old_values=snapshot(developer, AUDITED_FIELDS),
    )


def last_card(developer: Developer, reason: str | None = None) -> tuple[object, str] | None:
    """The developer's most recent card that was taken back (for `reason` only, if given),
    and whether it can go back to them: "FREE", or "TAKEN" (someone else holds it now),
    "BLOCKED" or "RETIRED". None: no such card."""
    from apps.rfid.models import CardStatus, RFIDCardAssignment

    ended = RFIDCardAssignment.objects.filter(developer=developer, unassigned_at__isnull=False)
    if reason:
        ended = ended.filter(end_reason=reason)
    last = ended.select_related("card").order_by("-unassigned_at", "-id").first()
    if last is None:
        return None
    card = last.card
    if card.status != CardStatus.ACTIVE:
        return card, card.status
    if RFIDCardAssignment.objects.filter(card=card, unassigned_at__isnull=True).exists():
        return card, "TAKEN"
    return card, "FREE"


def card_taken_on_delete(developer: Developer) -> tuple[object, str] | None:
    """The card taken back when the developer was deleted (see `last_card`)."""
    return last_card(developer, AssignmentEndReason.DEVELOPER_DELETED)


@transaction.atomic
def restore_card(*, actor, developer: Developer):
    """Give the developer back their last card, if they have none now and it is still
    free (a retired one is restored first). Returns the card, or raises with why not."""
    from rest_framework.exceptions import ValidationError

    from apps.rfid.models import RFIDCardAssignment
    from apps.rfid.services import assign_card, change_card_status

    if RFIDCardAssignment.objects.filter(developer=developer, unassigned_at__isnull=True).exists():
        raise ValidationError({"card": [_("This developer already has a card.")]})
    old = last_card(developer)
    if old is None:
        raise ValidationError({"card": [_("This developer never had a card.")]})
    card, state = old
    if state == "RETIRED":
        card = change_card_status(actor=actor, card=card, transition="restore", reason="")
        state = last_card(developer)[1]
    if state != "FREE":
        why = {
            "TAKEN": _("Their last card is held by someone else now."),
            "BLOCKED": _("Their last card is blocked."),
            "RETIRED": _("Their last card is retired."),
        }
        raise ValidationError({"card": [why.get(state, _("Their last card cannot be used."))]})
    assign_card(actor=actor, card=card, developer=developer)
    return card


@transaction.atomic
def restore_developer(*, actor, developer: Developer, with_card: bool = False) -> tuple:
    """Undo a delete: the developer is back with everything they had. Their card was taken
    back on delete: with `with_card` it is given back if still free. Refused while someone
    else uses the same employee number. Returns (developer, card given back or None)."""
    from rest_framework.exceptions import ValidationError

    developer = Developer.all_objects.select_for_update().get(pk=developer.pk)
    if developer.deleted_at is None:
        return developer
    if Developer.objects.filter(employee_number__iexact=developer.employee_number).exists():
        raise ValidationError(
            {
                "employee_number": [
                    _("Another developer uses employee number %(number)s now.")
                    % {"number": developer.employee_number}
                ]
            }
        )
    developer.deleted_at = None
    developer.save(update_fields=["deleted_at", "updated_at"])
    record_audit(
        "developer.restored",
        actor=actor,
        entity=developer,
        new_values=snapshot(developer, AUDITED_FIELDS),
    )
    given = None
    if with_card:
        old = card_taken_on_delete(developer)
        if old is not None and old[1] == "FREE" and developer.status != DeveloperStatus.TERMINATED:
            from apps.rfid.services import assign_card, change_card_status

            assign_card(actor=actor, card=old[0], developer=developer)
            given = old[0]
    return developer, given


@transaction.atomic
def set_photo(*, actor, developer: Developer, photo) -> Developer:
    """Set (`photo`) or remove (`None`) the developer's photo; the old file is deleted once
    the change is saved."""
    developer = Developer.objects.select_for_update().get(pk=developer.pk)
    old = developer.photo.name if developer.photo else ""
    if photo is None:
        developer.photo = ""
    else:
        developer.photo.save(photo.name, photo, save=False)
    developer.save(update_fields=["photo", "updated_at"])
    if old:
        storage = Developer._meta.get_field("photo").storage
        transaction.on_commit(lambda: storage.delete(old))
    record_audit(
        "developer.photo_changed" if photo is not None else "developer.photo_removed",
        actor=actor,
        entity=developer,
        old_values={"photo": old or None},
        new_values={"photo": developer.photo.name or None},
    )
    return developer
