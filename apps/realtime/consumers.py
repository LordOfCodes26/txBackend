from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .notify import counter_group
from .tickets import redeem_ticket

CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404


@database_sync_to_async
def _authorize(position_id: int, ticket: str, device_key: str):
    """Return (allowed, close_code, who). Who may watch a counter:
    - its TILL device (Device key),
    - the counter's own active seller, or users with purchase.view (ticket)."""
    from apps.accounts.models import User
    from apps.rfid.authentication import device_for_key
    from apps.rfid.models import DevicePurpose
    from apps.sellers.access import acting_seller
    from apps.sellers.models import ServicePosition

    position = ServicePosition.objects.filter(pk=position_id).first()

    if device_key:
        device = device_for_key(device_key)
        if device is None:
            return False, CLOSE_UNAUTHENTICATED, None
        if device.purpose != DevicePurpose.TILL or device.service_position_id != position_id:
            return False, CLOSE_FORBIDDEN, None
        return True, None, f"device:{device.code}"

    user_id = redeem_ticket(ticket)
    user = User.objects.filter(pk=user_id, is_active=True).first() if user_id else None
    if user is None:
        return False, CLOSE_UNAUTHENTICATED, None
    if position is None:
        return False, CLOSE_NOT_FOUND, None
    if user.has_rbac_perm("purchase.view"):
        return True, None, f"user:{user.pk}"
    seller = acting_seller(user)
    if seller is not None and seller.pk == position.seller_id:
        return True, None, f"user:{user.pk}"
    return False, CLOSE_FORBIDDEN, None


class CounterConsumer(AsyncJsonWebsocketConsumer):
    """Live events for one counter: ws(s)://<host>/ws/counters/<service_position_id>/

    Browsers connect with `?ticket=<ticket from POST /api/v1/realtime/ticket/>`.
    Till programs connect with the header `Authorization: Device <api key>`.
    The server only sends; anything the client sends is ignored.
    """

    async def connect(self):
        self.position_id = self.scope["url_route"]["kwargs"]["position_id"]
        query = parse_qs(self.scope.get("query_string", b"").decode())
        ticket = (query.get("ticket") or [""])[0]
        headers = dict(self.scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode(errors="ignore").split()
        device_key = auth[1] if len(auth) == 2 and auth[0].lower() == "device" else ""

        allowed, code, who = await _authorize(self.position_id, ticket, device_key)
        if not allowed:
            # Accept-then-close so browsers receive the reason code.
            await self.accept()
            await self.close(code=code)
            return
        self.group = counter_group(self.position_id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        await self.send_json({"type": "connected", "service_position": self.position_id, "as": who})

    async def disconnect(self, code):
        if hasattr(self, "group"):
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get("type") == "ping":
            await self.send_json({"type": "pong"})

    async def counter_event(self, event):
        await self.send_json(event["payload"])
