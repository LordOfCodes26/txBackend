"""COM port -> TCP bridge for the Master / Pay card readers.

Reads `$ID:...,TYPE:...,UID=...$` frames from the readers' serial ports, sends each one
to the server on a fresh TCP connection, and writes the reply (CARD_OK / CARD_NO /
CARD_DENIED + CR LF) back to the reader that sent it.

By default it listens on every CH341 (WCH) USB serial adapter, the chip in the readers,
and checks for new ones every 3 seconds, so a reader works on whatever COM number Windows
gives it: unplugging, another USB socket or a reboot need nothing done. Other serial
devices (receipt printers, scales) are left alone. The frame carries the reader's ID, so the bridge doesn't
need to know which reader is on which port.

One connection per scan means nothing can go stale: server idle timeout (300 s),
server restarts and router/NAT timeouts no longer matter.

    pip install pyserial
    python reader_bridge.py --server 1.2.3.4:9100                 all CH341 adapters
    python reader_bridge.py --server 1.2.3.4:9100 --exclude COM7  ... except this one
    python reader_bridge.py --server 1.2.3.4:9100 --any-usb       all USB COM ports
    python reader_bridge.py --server 1.2.3.4:9100 --port COM3     only these ports
    python reader_bridge.py --list-ports
"""

import argparse
import logging
import socket
import threading
import time

import serial  # pyserial
from serial.tools import list_ports

log = logging.getLogger("bridge")
MARK = b"$"
SCAN_SECONDS = 3
WCH_VIDS = {0x1A86, 0x4348}  # CH340 / CH341 USB vendor IDs


def send_frame(host: str, port: int, frame: bytes, timeout: float) -> bytes:
    """Send one frame on its own connection; return the reply line. Retries 3 times."""
    for attempt in range(1, 4):
        try:
            with socket.create_connection((host, port), timeout=timeout) as sock:
                sock.sendall(frame)
                reply = b""
                while not reply.endswith(b"\n"):
                    chunk = sock.recv(256)
                    if not chunk:
                        break
                    reply += chunk
                if reply:
                    return reply
        except OSError as exc:
            log.warning("send attempt %d failed: %s", attempt, exc)
        time.sleep(attempt)
    return b""


def take_frames(buf: bytearray) -> list[bytes]:
    """Pull complete `$...$` frames out of `buf`; keep any partial frame for later."""
    frames = []
    while True:
        start = buf.find(MARK)
        end = buf.find(MARK, start + 1) if start >= 0 else -1
        if end < 0:
            if start < 0:
                buf.clear()
            elif start > 0:
                del buf[:start]
            return frames
        frame = bytes(buf[start : end + 1])
        del buf[: end + 1]
        if len(frame) > 2:
            frames.append(frame)


def serve_port(ser: serial.Serial, host: str, tcp_port: int, timeout: float) -> None:
    """Forward one open port's frames until the port goes away."""
    name = ser.port
    buf = bytearray()
    try:
        while True:
            data = ser.read(ser.in_waiting or 1)
            buf += data.replace(b"\x00", b"")  # line noise
            for frame in take_frames(buf):
                log.info("%s -> %s", name, frame.decode(errors="replace"))
                reply = send_frame(host, tcp_port, frame, timeout)
                if not reply:
                    log.error("%s no reply from server, answering CARD_NO", name)
                    reply = b"CARD_NO\r\n"
                log.info("%s <- %s", name, reply.decode(errors="replace").strip())
                ser.write(reply)
    except (serial.SerialException, OSError) as exc:
        log.warning("%s closed (%s)", name, exc)
    finally:
        try:
            ser.close()
        except Exception:
            pass


def wanted_ports(only: set[str], exclude: set[str], any_usb: bool) -> set[str]:
    if only:
        return only
    found = {
        p.device
        for p in list_ports.comports()
        if p.vid is not None and (any_usb or p.vid in WCH_VIDS)
    }
    return {p for p in found if p.upper() not in exclude}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--server", help="host:port of the RFID TCP listener")
    p.add_argument("--port", action="append", default=[],
                   help="only this serial port (repeatable); default: every CH341 adapter")
    p.add_argument("--exclude", action="append", default=[],
                   help="never open this port, e.g. a receipt printer (repeatable)")
    p.add_argument("--any-usb", action="store_true",
                   help="every USB serial port, not only CH341 adapters")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--timeout", type=float, default=5.0, help="seconds per TCP attempt")
    p.add_argument("--list-ports", action="store_true", help="show the COM ports and exit")
    args = p.parse_args()
    if args.list_ports:
        for info in list_ports.comports():
            if info.vid in WCH_VIDS:
                kind = "reader"
            elif info.vid is not None:
                kind = "USB"
            else:
                kind = ""
            print(f"{info.device:8} {kind:7} {info.description}")
        return
    if not args.server:
        p.error("--server is required")
    host, tcp_port = args.server.rsplit(":", 1)
    only = set(args.port)
    exclude = {e.upper() for e in args.exclude}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("bridge to %s, ports: %s", args.server,
             ", ".join(sorted(only)) if only
             else "all USB serial ports" if args.any_usb else "all CH341 adapters")

    active: dict[str, threading.Thread] = {}
    failing: dict[str, str] = {}  # port -> last open error, logged once
    while True:
        for name in [n for n, t in active.items() if not t.is_alive()]:
            del active[name]
        for name in sorted(wanted_ports(only, exclude, args.any_usb) - active.keys()):
            try:
                ser = serial.Serial(name, args.baud, timeout=1)
            except serial.SerialException as exc:
                if failing.get(name) != str(exc):
                    log.warning("%s not available (%s), retrying", name, exc)
                    failing[name] = str(exc)
                continue
            failing.pop(name, None)
            log.info("%s open", name)
            thread = threading.Thread(
                target=serve_port, args=(ser, host, int(tcp_port), args.timeout), daemon=True
            )
            thread.start()
            active[name] = thread
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
