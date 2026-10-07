"""Push events to WebSocket listeners: a counter (service position) or building occupancy.

Best effort: events are sent after the database transaction commits, and a failure to
reach Redis is logged, never raised. Clients should still refetch on reconnect.
"""

import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)


def counter_group(position_id: int) -> str:
    return f"counter.{position_id}"


OCCUPANCY_GROUP = "occupancy"


def _group_send(group: str, payload: dict) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(group, {"type": "push.event", "payload": payload})
    except Exception:
        logger.warning(
            "Realtime %s event for %s not delivered", payload.get("type"), group, exc_info=True
        )


def _send(position_id: int, event_type: str, data: dict) -> None:
    _group_send(
        counter_group(position_id),
        {
            "type": event_type,
            "service_position": position_id,
            "sent_at": timezone.now().isoformat(),
            "data": data,
        },
    )


def notify_attendance_scan(event) -> None:
    """A card was scanned at a building door (accepted or not)."""
    from apps.rfid.serializers import ScanResponseSerializer

    def send():
        data = dict(ScanResponseSerializer(event).data)
        data.pop("client_event_id", None)
        data.pop("purchase", None)
        building = event.device.building
        data["device_code"] = event.device.code
        data["device_name"] = event.device.name or event.device.code
        data["building"] = (
            None
            if building is None
            else {"id": building.pk, "code": building.code, "name": building.name}
        )
        _group_send(OCCUPANCY_GROUP, {"type": "attendance", "data": data})

    transaction.on_commit(send)


def notify_occupancy() -> None:
    """Push the current building counts after an attendance change."""

    def send():
        from apps.attendance.occupancy import occupancy

        _group_send(OCCUPANCY_GROUP, {"type": "occupancy", "data": occupancy()})

    transaction.on_commit(send)


def notify_card_tapped(event) -> None:
    """A card was tapped on a counter's TILL reader (accepted or not)."""
    from apps.rfid.serializers import ScanResponseSerializer

    def send():
        # Show the tap on the counter of the purchase it was attached to, or (e.g. a blocked
        # card, which is never attached) of the purchase waiting on this reader. With no such
        # purchase, only the device's own reply reports the tap.
        from apps.purchases.models import Purchase
        from apps.purchases.services import draft_for_reader

        purchase = Purchase.objects.filter(presented_event=event).first() or draft_for_reader(
            event.device_id
        )
        if purchase is None:
            return
        data = dict(ScanResponseSerializer(event).data)
        data.pop("client_event_id", None)
        _send(purchase.service_position_id, "card_tapped", data)

    transaction.on_commit(send)


def notify_purchase(purchase_id: int, position_id: int, event_type: str) -> None:
    """purchase_updated / purchase_confirmed / purchase_cancelled with the full purchase."""

    def send():
        from apps.purchases.serializers import PurchaseSerializer
        from apps.purchases.views import PurchaseViewSet

        purchase = PurchaseViewSet.queryset.filter(pk=purchase_id).first()
        if purchase is None:
            return
        _send(position_id, event_type, dict(PurchaseSerializer(purchase).data))

    transaction.on_commit(send)
