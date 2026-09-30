from django.db import models

from common.context import get_request_context

from .models import AuditLog


def record_audit(
    action: str,
    *,
    actor=None,
    entity: models.Model | None = None,
    entity_type: str | None = None,
    entity_id=None,
    old_values: dict | None = None,
    new_values: dict | None = None,
) -> AuditLog:
    """Write an audit entry.

    Call this inside the same `transaction.atomic()` block as the change it describes,
    so the change and its audit record commit or roll back together.
    """
    if entity is not None:
        entity_type = entity_type or entity._meta.label_lower
        entity_id = entity.pk if entity_id is None else entity_id
    if not entity_type or entity_id is None:
        raise ValueError("record_audit needs `entity` or both `entity_type` and `entity_id`.")

    if actor is not None and not actor.is_authenticated:
        actor = None
    ctx = get_request_context()
    return AuditLog.objects.create(
        actor=actor,
        actor_email=actor.email if actor else "",
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        old_values=old_values,
        new_values=new_values,
        ip_address=ctx.ip_address if ctx else None,
        user_agent=ctx.user_agent if ctx else "",
        request_id=ctx.request_id if ctx else "",
    )


def snapshot(instance: models.Model, fields) -> dict:
    """JSON-friendly values of `fields`; foreign keys are stored as their id."""
    values = {}
    for name in fields:
        field = instance._meta.get_field(name)
        values[name] = getattr(instance, field.attname)
    return values


def diff(before: dict, after: dict) -> tuple[dict, dict]:
    """(old_values, new_values) restricted to keys whose value changed."""
    changed = [k for k in after if before.get(k) != after[k]]
    return {k: before.get(k) for k in changed}, {k: after[k] for k in changed}
