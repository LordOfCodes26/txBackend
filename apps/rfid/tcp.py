"""Raw TCP listener for the RFID devices (doors, till readers, card assign readers).

Every frame starts and ends with "$". The devices send text frames and get a text reply
(CR LF terminated):

    $ID:Door1,TYPE:Input,UID=DC62B3E3$      doors: TYPE Input (in) or Output (out)
    $ID:Reader1,TYPE:Pay,UID=DC62B3E3$      till readers (`ID:ID:Reader1` is accepted too)
    $ID:Master1,TYPE:Master,UID=DC62B3E3$   card assign readers

    CARD_OK       accepted: door opens; till: a purchase waiting for a card took it;
                  card assign: card known
    CARD_NO       unknown card; till: also any card no purchase took; card assign: new
                  card (registered by this tap)
    CARD_DENIED   doors only: known card that may not enter (blocked, retired, not
                  assigned, developer not active)

A frame the server can't use (unknown device, bad UID, ...) gets CARD_NO and a log line.
JSON frames (`${"ID": "Door1", "Type": "in", "UID": "04A2B3C4"}$`) are still accepted and
get a JSON reply (`${"result": "ACCEPTED", "accepted": true, ...}$`).

A connection may carry one scan or many (kept open). Bytes outside frames (such as CR LF)
are ignored.

Identification (no keys): a door by its `ID` and the sender's address, which must equal
the device's `allowed_ip`; several door units may share an ID, each registered as its own
device with its own IP. There is no proxy in front of this port, so the peer address is the
real sender. Till and card assign readers by their `ID` alone.

Scans go through `services.record_scan`, exactly like HTTP scans (attendance, occupancy,
live dashboard events, till payments).
"""

import asyncio
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass

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
    # default=str: translated texts (lazy strings) become plain text.
    body = json.dumps(payload, separators=(",", ":"), default=str)
    return FRAME_MARK + body.encode() + FRAME_MARK


def _error(message: str) -> dict:
    with translation.override(settings.DEVICE_LANGUAGE):
        return {"result": "ERROR", "accepted": False, "error": str(message)}


# Replies to the devices' text frames (each followed by CR LF).
CARD_OK = "CARD_OK"  # accepted: the door opens, the till takes the card, the card is known
CARD_NO = "CARD_NO"  # unknown card (or the frame/device could not be used)
CARD_DENIED = "CARD_DENIED"  # doors: a known card that may not enter


def parse_text_frame(frame: bytes) -> dict[str, str]:
    """`ID:Door1,TYPE:Input,UID=DC62B3E3` -> {"id": "Door1", "type": "Input", "uid": ...}.

    Each field is `key:value` or `key=value`. Some readers repeat the key in the value
    (`ID:ID:Reader1`); the repeat is dropped.
    """
    fields = {}
    for part in frame.decode("utf-8", errors="replace").split(","):
        cut = min((i for i in (part.find(":"), part.find("=")) if i >= 0), default=-1)
        if cut < 0:
            continue
        key, value = part[:cut].strip().lower(), part[cut + 1 :].strip()
        while value[: len(key) + 1].lower() in (f"{key}:", f"{key}="):
            value = value[len(key) + 1 :].strip()
        if key:
            fields[key] = value
    return fields


@dataclass
class _Trace:
    """What handling a frame found out, for the TCP log."""

    code: str = ""
    device: object = None
    event: object = None
    outcome: str = "OK"
    note: str = ""


def handle_frame(frame: bytes, peer_ip: str) -> dict | str:
    """Authenticate, validate and record one frame, and log request and response.

    A JSON frame (`{...}`) gets a JSON reply payload, with texts in DEVICE_LANGUAGE. A
    text frame (`ID:...,TYPE:...,UID=...`) gets CARD_OK, CARD_NO or CARD_DENIED.
    """
    started = time.monotonic()
    trace = _Trace()
    is_json = frame.lstrip().startswith(b"{")
    with translation.override(settings.DEVICE_LANGUAGE):
        try:
            if is_json:
                reply = _handle_json_frame(frame, peer_ip, trace)
            else:
                reply = _handle_text_frame(frame, peer_ip, trace) + "\r\n"
        except Exception as exc:
            logger.exception("RFID TCP: error handling frame from %s", peer_ip)
            trace.outcome, trace.note = "ERROR", f"{type(exc).__name__}: {exc}"[:300]
            reply = _error(_("Server error.")) if is_json else CARD_NO + "\r\n"
    response = reply if isinstance(reply, str) else encode(reply).decode()
    log_tcp(
        peer_ip,
        frame.decode("utf-8", errors="replace"),
        response,
        trace,
        int((time.monotonic() - started) * 1000),
    )
    return reply


def log_tcp(peer_ip, request: str, response: str, trace: _Trace, duration_ms: int = 0) -> None:
    """One row of the TCP log. Never lets logging break the answer to the device."""
    from .models import TCPFrameLog

    try:
        TCPFrameLog.objects.create(
            peer_ip=peer_ip or None,
            request=request,
            response=response,
            device_code=trace.code[:50],
            device=trace.device,
            event=trace.event,
            outcome=trace.outcome,
            note=trace.note[:300],
            duration_ms=duration_ms,
        )
    except Exception:
        logger.exception("RFID TCP: could not write the TCP log")


def prune_tcp_log() -> int:
    """Delete TCP log rows older than RFID_TCP_LOG_DAYS. Returns how many."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import TCPFrameLog

    limit = timezone.now() - timedelta(days=settings.RFID_TCP_LOG_DAYS)
    deleted, _rows = TCPFrameLog.objects.filter(received_at__lt=limit).delete()
    return deleted


def _record(device, data: dict, peer_ip: str):
    """Validate `data` for `device` and record the scan. Returns the event."""
    from . import services
    from .serializers import ScanSerializer

    serializer = ScanSerializer(data=data, context={"device": device})
    serializer.is_valid(raise_exception=True)
    event, _created = services.record_scan(
        device=device,
        uid=serializer.validated_data["uid"],
        event_time=serializer.validated_data.get("event_time"),
        client_event_id=serializer.validated_data["client_event_id"],
        direction=serializer.validated_data["direction"],
        source_ip=peer_ip,
    )
    return event


def _handle_text_frame(frame: bytes, peer_ip: str, trace: _Trace) -> str:
    from rest_framework.exceptions import ValidationError

    from .authentication import device_for_id
    from .models import DevicePurpose, RFIDCard, ScanResult, normalize_uid

    fields = parse_text_frame(frame)
    code = trace.code = fields.get("id", "")
    device = trace.device = device_for_id(code, peer_ip)
    if device is None:
        logger.warning("RFID TCP: rejected ID=%r from %s (no such device/IP)", code, peer_ip)
        trace.outcome, trace.note = "REJECTED", "No active device with this ID for this address."
        return CARD_NO
    known = RFIDCard.objects.filter(uid=normalize_uid(fields.get("uid", ""))).exists()
    try:
        event = trace.event = _record(device, fields, peer_ip)
    except ValidationError as exc:
        logger.warning("RFID TCP: invalid frame from %s (%s): %s", peer_ip, code, exc.detail)
        trace.outcome, trace.note = "INVALID", json.dumps(exc.detail, default=str)
        return CARD_NO
    trace.note = event.result

    if device.purpose == DevicePurpose.ENROLL:
        return CARD_OK if known else CARD_NO  # a new card is registered by this tap
    if device.purpose == DevicePurpose.TILL:
        from apps.purchases.models import Purchase

        # OK only when a purchase waiting for a card took the tap.
        taken = Purchase.objects.filter(presented_event=event).exists()
        return CARD_OK if taken else CARD_NO
    if event.result in (ScanResult.ACCEPTED, ScanResult.DUPLICATE):
        return CARD_OK
    if device.purpose == DevicePurpose.ATTENDANCE and event.result != ScanResult.UNKNOWN_CARD:
        return CARD_DENIED  # blocked, retired, not assigned or developer not active
    return CARD_NO


def _handle_json_frame(frame: bytes, peer_ip: str, trace: _Trace) -> dict:
    from rest_framework.exceptions import ValidationError

    from .authentication import device_for_id
    from .models import DevicePurpose
    from .serializers import ScanResponseSerializer

    trace.outcome = "INVALID"
    try:
        data = json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        trace.note = "Frame is not valid JSON."
        return _error(_("Frame is not valid JSON."))
    if not isinstance(data, dict):
        trace.note = "Frame must be a JSON object."
        return _error(_("Frame must be a JSON object."))

    fields = {str(k).lower(): v for k, v in data.items()}
    code = trace.code = str(fields.get("id") or fields.get("device_id") or "").strip()
    if not code:
        trace.note = "Missing ID."
        return _error(_("Missing ID."))
    device = trace.device = device_for_id(code, peer_ip)
    if device is None:
        logger.warning("RFID TCP: rejected ID=%r from %s (no such device/IP)", code, peer_ip)
        trace.outcome, trace.note = "REJECTED", "No active device with this ID for this address."
        return _error(_("No door device with this ID is registered for this IP."))
    try:
        event = trace.event = _record(device, data, peer_ip)
    except ValidationError as exc:
        trace.note = json.dumps(exc.detail, default=str)
        return _error(json.dumps(exc.detail, default=str))
    trace.outcome, trace.note = "OK", event.result
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
        self._log = database_sync_to_async(log_tcp)
        self._pruner: asyncio.Task | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, self.host, self.port)
        sockets = ", ".join(str(s.getsockname()) for s in self.server.sockets)
        logger.info("RFID TCP listener on %s", sockets)
        self._pruner = asyncio.create_task(self._prune_hourly())

    async def _prune_hourly(self) -> None:
        """Keep the TCP log to RFID_TCP_LOG_DAYS."""
        while True:
            try:
                await database_sync_to_async(prune_tcp_log)()
            except Exception:
                logger.exception("RFID TCP: could not prune the TCP log")
            await asyncio.sleep(3600)

    async def _refused(self, peer_ip: str, request: str, reply: dict, note: str) -> None:
        trace = _Trace(outcome="REFUSED", note=note)
        await self._log(peer_ip, request, encode(reply).decode(), trace)

    async def serve_forever(self) -> None:
        await self.start()
        async with self.server:
            await self.server.serve_forever()

    async def close(self) -> None:
        if self._pruner is not None:
            self._pruner.cancel()
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
            reply = _error(_("Too many connections."))
            await self._refused(peer_ip, "", reply, "Too many connections from this address.")
            writer.write(encode(reply))
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
                    reply = _error(_("Frame too large."))
                    limit = settings.RFID_TCP_MAX_FRAME_BYTES
                    request = data[:500].decode(errors="replace")
                    note = f"Packet over {limit} bytes."
                    await self._refused(peer_ip, request, reply, note)
                    writer.write(encode(reply))
                    break
                for frame in frames:
                    text = not frame.lstrip().startswith(b"{")
                    try:
                        reply = await self._handle_frame(frame, peer_ip)
                    except Exception:
                        logger.exception("RFID TCP: error handling frame from %s", peer_ip)
                        reply = CARD_NO + "\r\n" if text else _error(_("Server error."))
                    writer.write(reply.encode() if isinstance(reply, str) else encode(reply))
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
