from django.db import transaction
from django.utils import timezone

from apps.audit.services import diff, record_audit, snapshot
from apps.finance.services import open_account
from apps.rfid.models import AssignmentEndReason
from apps.rfid.services import end_developer_assignment

from .exceptions import DeveloperHasReports
from .models import Developer, DeveloperStatus

AUDITED_FIELDS = [
    "user",
    "employee_number",
    "full_name",
    "phone",
    "home_address",
    "birthday",
    "department",
    "position_title",
    "manager",
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
    return developer


@transaction.atomic
def delete_developer(*, actor, developer: Developer) -> None:
    developer = Developer.objects.select_for_update().get(pk=developer.pk)
    if developer.reports.exists():
        raise DeveloperHasReports()
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
