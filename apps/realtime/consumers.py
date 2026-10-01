from urllib.parse import parse_qs

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from .notify import OCCUPANCY_GROUP, counter_group
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
    from apps.sellers.access import acting_positions, acting_seller
    from apps.sellers.models import ServicePosition

    position = ServicePosition.objects.filter(pk=position_id).first()

    if device_key:
        device = device_for_key(device_key)
        if device is None:
            return False, CLOSE_UNAUTHENTICATED, None
        # Readers aren't tied to a counter, so devices can't follow a counter's channel;
        # a till program gets the result of each tap in the reply to its request.
        return False, CLOSE_FORBIDDEN, None

    user_id = redeem_ticket(ticket)
    user = User.objects.filter(pk=user_id, is_active=True).first() if user_id else None
    if user is None:
        return False, CLOSE_UNAUTHENTICATED, None
    if position is None:
        return False, CLOSE_NOT_FOUND, None
    if user.has_rbac_perm("purchase.view"):
        from apps.rfid.scope import building_scope

        scope = building_scope(user, "purchase.view")
        if scope is None or position.building_id in scope:
            return True, None, f"user:{user.pk}"
    seller, positions = acting_seller(user), acting_positions(user)
    if (
        seller is not None
        and seller.pk == position.seller_id
        and (positions is None or position.pk in positions)
    ):
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

    async def push_event(self, event):
        await self.send_json(event["payload"])


@database_sync_to_async
def _authorize_occupancy(ticket: str):
    from apps.accounts.models import User

    user_id = redeem_ticket(ticket)
    user = User.objects.filter(pk=user_id, is_active=True).first() if user_id else None
    if user is None:
        return False, CLOSE_UNAUTHENTICATED, None
    if not user.has_rbac_perm("attendance.view"):
        return False, CLOSE_FORBIDDEN, None
    from apps.rfid.scope import building_scope

    return True, None, building_scope(user, "attendance.view")


@database_sync_to_async
def _occupancy_snapshot(buildings=None):
    from apps.attendance.occupancy import occupancy

    return occupancy(buildings)


def _for_buildings(payload: dict, buildings) -> dict | None:
    """A building manager only gets their buildings' counts and scans."""
    if buildings is None:
        return payload
    if payload.get("type") == "occupancy":
        data = dict(payload["data"])
        data["buildings"] = [b for b in data["buildings"] if b["id"] in buildings]
        data["total"] = sum(b["count"] for b in data["buildings"])
        data["unknown_building"] = 0
        return {**payload, "data": data}
    building = (payload.get("data") or {}).get("building")
    return payload if building and building["id"] in buildings else None


class OccupancyConsumer(AsyncJsonWebsocketConsumer):
    """Live building counts: ws(s)://<host>/ws/occupancy/?ticket=<ticket>.

    Sends the current counts on connect, then again after every attendance change.
    Requires `attendance.view`.
    """

    async def connect(self):
        query = parse_qs(self.scope.get("query_string", b"").decode())
        allowed, code, self.buildings = await _authorize_occupancy((query.get("ticket") or [""])[0])
        await self.accept()
        if not allowed:
            await self.close(code=code)
            return
        self.joined = True
        await self.channel_layer.group_add(OCCUPANCY_GROUP, self.channel_name)
        await self.send_json(
            {"type": "occupancy", "data": await _occupancy_snapshot(self.buildings)}
        )

    async def disconnect(self, code):
        if getattr(self, "joined", False):
            await self.channel_layer.group_discard(OCCUPANCY_GROUP, self.channel_name)

    async def receive_json(self, content, **kwargs):
        if content.get("type") == "ping":
            await self.send_json({"type": "pong"})

    async def push_event(self, event):
        payload = _for_buildings(event["payload"], self.buildings)
        if payload is not None:
            await self.send_json(payload)
