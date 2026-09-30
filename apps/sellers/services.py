from django.db import transaction
from django.utils import timezone

from apps.audit.services import diff, record_audit, snapshot
from apps.seller_finance.services import open_seller_account

from .exceptions import PositionHasGoods
from .models import Seller, ServicePosition

SELLER_FIELDS = ["user", "name", "contact_name", "email", "phone", "status", "notes"]
POSITION_FIELDS = ["seller", "name", "location", "is_active"]


@transaction.atomic
def create_seller(*, actor, **data) -> Seller:
    seller = Seller.objects.create(**data)
    open_seller_account(seller)
    record_audit(
        "seller.created", actor=actor, entity=seller, new_values=snapshot(seller, SELLER_FIELDS)
    )
    return seller


@transaction.atomic
def update_seller(*, actor, seller: Seller, **changes) -> Seller:
    seller = Seller.objects.select_for_update().get(pk=seller.pk)
    before = snapshot(seller, SELLER_FIELDS)
    for field, value in changes.items():
        setattr(seller, field, value)
    seller.save()
    old, new = diff(before, snapshot(seller, SELLER_FIELDS))
    if new:
        record_audit("seller.updated", actor=actor, entity=seller, old_values=old, new_values=new)
    return seller


@transaction.atomic
def create_position(*, actor, **data) -> ServicePosition:
    position = ServicePosition.objects.create(**data)
    record_audit(
        "seller.position_created",
        actor=actor,
        entity=position,
        new_values=snapshot(position, POSITION_FIELDS),
    )
    return position


@transaction.atomic
def update_position(*, actor, position: ServicePosition, **changes) -> ServicePosition:
    position = ServicePosition.objects.select_for_update().get(pk=position.pk)
    before = snapshot(position, POSITION_FIELDS)
    for field, value in changes.items():
        setattr(position, field, value)
    position.save()
    old, new = diff(before, snapshot(position, POSITION_FIELDS))
    if new:
        record_audit(
            "seller.position_updated", actor=actor, entity=position, old_values=old, new_values=new
        )
    return position


@transaction.atomic
def delete_position(*, actor, position: ServicePosition) -> None:
    position = ServicePosition.objects.select_for_update().get(pk=position.pk)
    if position.goods.exists():
        raise PositionHasGoods()
    position.deleted_at = timezone.now()
    position.save(update_fields=["deleted_at", "updated_at"])
    record_audit(
        "seller.position_deleted",
        actor=actor,
        entity=position,
        old_values=snapshot(position, POSITION_FIELDS),
    )
