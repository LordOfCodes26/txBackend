"""Raw TCP listener for door devices that can't speak HTTP.

Wire format (both directions): JSON framed by a "$" at the start and the end:

    ${"ID": "Door1", "Type": "in", "UID": "04A2B3C4"}$

A connection may carry one scan or many (kept open). The server answers every frame:

    ${"result": "ACCEPTED", "accepted": true, "direction": "IN", "message": "Welcome, Ada"}$
    ${"result": "ERROR", "accepted": false, "error": "..."}$

Authentication:
- doors: fixed IP. The peer address must equal the `allowed_ip` of the active ATTENDANCE
  device whose code is `ID`. There is no proxy in front of this port, so the peer address
  is the real sender.
- till readers: the frame carries `SN` (serial number) and `ID`; both must match an
  active TILL device (same rule as key-less HTTP, with lockout after repeated failures).

Scans go through `services.record_scan`, exactly like HTTP scans (attendance, occupancy,
live dashboard events).
"""

import asyncio
import json
import logging
from collections import Counter

from channels.db import database_sync_to_async
from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

FRAME_MARK = b"$"


class FrameTooLarge(Exception):
    pass


class FrameDecoder:
    """Incremental `$...$` frame decoder for a TCP byte stream.

    Bytes outside frames (whitespace, line breaks, noise) are ignored. A frame that
    grows beyond `max_size` raises FrameTooLarge so the connection can be dropped.
    """

    def __init__(self, max_size: int):
        self.max_size = max_size
        self._buffer = bytearray()
        self._in_frame = False

    def feed(self, data: bytes) -> list[bytes]:
        frames = []
        for byte in data:
            if byte == FRAME_MARK[0]:
                if self._in_frame:
                    if self._buffer.strip():
                        frames.append(bytes(self._buffer))
                        self._buffer.clear()
                        self._in_frame = False
                    # "$$": an empty frame; treat the second "$" as a fresh start.
                else:
                    self._in_frame = True
                    self._buffer.clear()
                continue
            if self._in_frame:
                self._buffer.append(byte)
                if len(self._buffer) > self.max_size:
                    raise FrameTooLarge()
        return frames


def encode(payload: dict) -> bytes:
    return FRAME_MARK + json.dumps(payload, separators=(",", ":")).encode() + FRAME_MARK


def _error(message: str) -> dict:
    with translation.override(settings.DEVICE_LANGUAGE):
        return {"result": "ERROR", "accepted": False, "error": str(message)}


def handle_frame(frame: bytes, peer_ip: str) -> dict:
    """Authenticate, validate and record one frame. Returns the reply payload, with texts
    in DEVICE_LANGUAGE."""
    with translation.override(settings.DEVICE_LANGUAGE):
        return _handle_frame(frame, peer_ip)


def _handle_frame(frame: bytes, peer_ip: str) -> dict:
    from rest_framework.exceptions import ValidationError

    from . import services
    from .authentication import SNLockedOut, till_for_sn
    from .models import DevicePurpose, RFIDDevice
    from .serializers import ScanResponseSerializer, ScanSerializer

    try:
        data = json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error(_("Frame is not valid JSON."))
    if not isinstance(data, dict):
        return _error(_("Frame must be a JSON object."))

    fields = {str(k).lower(): v for k, v in data.items()}
    code = str(fields.get("id") or fields.get("device_id") or "").strip()
    if not code:
        return _error(_("Missing ID."))
    sn = str(fields.get("sn") or "").strip()
    if sn:
        # Till readers: serial number + ID.
        try:
            device = till_for_sn(code, sn, peer_ip)
        except SNLockedOut:
            return _error(_("Too many failed attempts; try again later."))
        if device is None:
            logger.warning("RFID TCP: rejected till ID=%r from %s (wrong SN)", code, peer_ip)
            return _error(_("Unknown till reader ID or serial number."))
    else:
        # Doors: fixed IP + ID.
        device = RFIDDevice.objects.filter(
            code__iexact=code,
            allowed_ip=peer_ip,
            is_active=True,
            purpose=DevicePurpose.ATTENDANCE,
        ).first()
        if device is None:
            logger.warning("RFID TCP: rejected ID=%r from %s (no matching door/IP)", code, peer_ip)
            return _error(_("No door device with this ID is registered for this IP."))

    serializer = ScanSerializer(data=data, context={"device": device})
    try:
        serializer.is_valid(raise_exception=True)
    except ValidationError as exc:
        return _error(json.dumps(exc.detail, default=str))
    event, _created = services.record_scan(
        device=device,
        uid=serializer.validated_data["uid"],
        event_time=serializer.validated_data.get("event_time"),
        client_event_id=serializer.validated_data["client_event_id"],
        direction=serializer.validated_data["direction"],
        source_ip=peer_ip,
    )
    body = ScanResponseSerializer(event).data
    reply = {
        "result": body["result"],
        "accepted": body["accepted"],
        "direction": body["direction"],
        "message": body["display_message"],
        "event_id": body["id"],
    }
    if device.purpose == DevicePurpose.TILL:
        reply["purchase"] = body["purchase"]
    return reply


class DoorTCPServer:
    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.connections: Counter[str] = Counter()
        self.server: asyncio.base_events.Server | None = None
        self._handle_frame = database_sync_to_async(handle_frame)

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, self.host, self.port)
        sockets = ", ".join(str(s.getsockname()) for s in self.server.sockets)
        logger.info("RFID TCP listener on %s", sockets)

    async def serve_forever(self) -> None:
        await self.start()
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer_ip = writer.get_extra_info("peername")[0]
        if (
            sum(self.connections.values()) >= settings.RFID_TCP_MAX_CONNECTIONS
            or self.connections[peer_ip] >= settings.RFID_TCP_MAX_CONNECTIONS_PER_IP
        ):
            logger.warning("RFID TCP: too many connections, refusing %s", peer_ip)
            writer.write(encode(_error(_("Too many connections."))))
            await self._close(writer)
            return
        self.connections[peer_ip] += 1
        decoder = FrameDecoder(settings.RFID_TCP_MAX_FRAME_BYTES)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(
                        reader.read(4096), timeout=settings.RFID_TCP_IDLE_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    break
                if not data:
                    break
                try:
                    frames = decoder.feed(data)
                except FrameTooLarge:
                    writer.write(encode(_error(_("Frame too large."))))
                    break
                for frame in frames:
                    try:
                        reply = await self._handle_frame(frame, peer_ip)
                    except Exception:
                        logger.exception("RFID TCP: error handling frame from %s", peer_ip)
                        reply = _error(_("Server error."))
                    writer.write(encode(reply))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.connections[peer_ip] -= 1
            if self.connections[peer_ip] <= 0:
                del self.connections[peer_ip]
            await self._close(writer)

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        try:
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass
