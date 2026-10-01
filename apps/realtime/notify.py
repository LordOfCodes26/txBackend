"""Push events to everyone watching a counter (service position).

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


def _send(position_id: int, event_type: str, data: dict) -> None:
    layer = get_channel_layer()
    if layer is None:
        return
    message = {
        "type": "counter.event",
        "payload": {
            "type": event_type,
            "service_position": position_id,
            "sent_at": timezone.now().isoformat(),
            "data": data,
        },
    }
    try:
        async_to_sync(layer.group_send)(counter_group(position_id), message)
    except Exception:
        logger.warning(
            "Realtime event %s for counter %s not delivered", event_type, position_id, exc_info=True
        )


def notify_card_tapped(event) -> None:
    """A card was tapped on a counter's TILL reader (accepted or not)."""
    from apps.rfid.serializers import ScanResponseSerializer

    position_id = event.device.service_position_id

    def send():
        data = dict(ScanResponseSerializer(event).data)
        data.pop("client_event_id", None)
        _send(position_id, "card_tapped", data)

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
