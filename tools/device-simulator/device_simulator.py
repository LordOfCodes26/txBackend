"""RFID device simulator: acts like the real doors, till readers and card assign readers.

Each tap opens a TCP connection to the server (port 9100), sends exactly the packet the
device sends and shows the server's one-line answer:

    $ID:Door1,TYPE:Input,UID=DC62B3E3$      ->  CARD_OK / CARD_NO / CARD_DENIED
    $ID:ID:Reader1,TYPE:Pay,UID=DC62B3E3$   ->  CARD_OK / CARD_NO
    $ID:Master1,TYPE:Master,UID=DC62B3E3$   ->  CARD_OK / CARD_NO

The server recognises a door unit by its ID *and* the address it sends from, so every
simulated device can have a "send from" IP. The app binds to that address when this PC has
it (add the addresses to the network card, see README.md); otherwise it can fall back to the
PC's normal address and says so in the log.

Window:          python device_simulator.py
Command line:    python device_simulator.py --tap Door1-1 DC62B3E3 [--server 192.168.100.10]
                 python device_simulator.py --list

Only the Python standard library is used (Tkinter for the window).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

APP_NAME = "RFID Device Simulator"
DEFAULT_SERVER = "192.168.100.10"
DEFAULT_PORT = 9100
TIMEOUT_SECONDS = 5
TYPES = ["Input", "Output", "Pay", "Master"]
# What each kind of device puts in its packet. {id}, {type} and {uid} are filled in.
TEMPLATES = {
    "Input": "ID:{id},TYPE:{type},UID={uid}",
    "Output": "ID:{id},TYPE:{type},UID={uid}",
    "Pay": "ID:ID:{id},TYPE:{type},UID={uid}",  # the till readers repeat "ID:"
    "Master": "ID:{id},TYPE:{type},UID={uid}",
}


@dataclass
class Device:
    name: str  # what the simulator calls it, e.g. Door1-1
    id: str  # the ID the device sends, e.g. Door1
    type: str  # Input, Output, Pay or Master
    source_ip: str = ""  # send from this address ("" = the PC's normal address)
    template: str = ""  # packet without the $ signs ("" = the default for the type)

    def packet(self, uid: str) -> str:
        template = self.template or TEMPLATES.get(self.type, TEMPLATES["Input"])
        return template.format(id=self.id, type=self.type, uid=uid)


def preset_devices() -> list[Device]:
    """The company's devices (doors: one device per unit, all units of a door share the ID)."""
    devices = [
        Device("Master1", "Master1", "Master"),
        Device("Master2", "Master2", "Master"),
        Device("Reader1", "Reader1", "Pay"),
        Device("Reader2", "Reader2", "Pay"),
    ]
    door1 = ["Input", "Input", "Output", "Output"]
    for n, kind in enumerate(door1, start=1):
        devices.append(Device(f"Door1-{n}", "Door1", kind, f"192.168.100.{150 + n}"))
    door2 = ["Input", "Input", "Input", "Output", "Output", "Output"]
    for n, kind in enumerate(door2, start=1):
        devices.append(Device(f"Door2-{n}", "Door2", kind, f"192.168.100.{200 + n}"))
    return devices


# --------------------------------------------------------------------------------- settings


def settings_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "DeviceSimulator" / "settings.json"


@dataclass
class Settings:
    server: str = DEFAULT_SERVER
    port: int = DEFAULT_PORT
    fallback: bool = True  # send from the PC's own address when a device's IP is missing
    devices: list[Device] | None = None
    uids: list[str] | None = None  # recently tapped card UIDs, newest first

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(settings_path().read_text(encoding="utf-8"))
            return cls(
                server=data.get("server", DEFAULT_SERVER),
                port=int(data.get("port", DEFAULT_PORT)),
                fallback=bool(data.get("fallback", True)),
                devices=[Device(**d) for d in data.get("devices", [])] or preset_devices(),
                uids=list(data.get("uids", [])),
            )
        except (OSError, ValueError, TypeError):
            return cls(devices=preset_devices(), uids=[])

    def save(self) -> None:
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def remember_uid(self, uid: str) -> None:
        self.uids = [uid] + [u for u in (self.uids or []) if u != uid][:19]


# ---------------------------------------------------------------------------------- network


@dataclass
class TapResult:
    packet: str
    reply: str  # CARD_OK / CARD_NO / CARD_DENIED, or "" when nothing came back
    sent_from: str  # the local address the server saw
    note: str = ""  # e.g. the device IP was missing and the PC's address was used
    error: str = ""  # connection problem
    millis: int = 0


def clean_uid(raw: str) -> str:
    """Card numbers are often written with spaces or colons: 04:A2 b3 -> 04A2B3."""
    return "".join(ch for ch in raw.strip() if ch not in " :-").upper()


def send_tap(server: str, port: int, device: Device, uid: str, fallback: bool = True) -> TapResult:
    """One tap: connect (from the device's IP), send the packet, read the reply line."""
    packet = device.packet(uid)
    result = TapResult(packet=packet, reply="", sent_from="")
    started = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT_SECONDS)
    try:
        if device.source_ip:
            try:
                sock.bind((device.source_ip, 0))
            except OSError:
                if not fallback:
                    result.error = (
                        f"This PC doesn't have {device.source_ip}; add it to the network card "
                        "(see README) or switch on the fallback."
                    )
                    return result
                result.note = f"this PC doesn't have {device.source_ip}, sent from its own IP"
                sock.close()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(TIMEOUT_SECONDS)
        try:
            sock.connect((server, port))
        except TimeoutError:
            result.error = (
                f"could not connect to {server}:{port} within {TIMEOUT_SECONDS} s "
                "(network or firewall; the packet was not sent)"
            )
            return result
        result.sent_from = sock.getsockname()[0]
        sock.sendall(b"$" + packet.encode() + b"$\r\n")
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(256)
            if not chunk:
                break
            data += chunk
        result.reply = data.decode(errors="replace").strip()
        if not result.reply:
            result.error = "the server closed the connection without an answer"
    except TimeoutError:
        result.error = f"connected and sent, but no answer within {TIMEOUT_SECONDS} s"
    except OSError as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()
        result.millis = int((time.monotonic() - started) * 1000)
    return result


def local_ips() -> list[str]:
    """The IPv4 addresses of this PC (best effort, no extra packages)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # the address used to reach the network
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        ips.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    ips.discard("0.0.0.0")
    return sorted(ips, key=lambda ip: (ip.startswith("127."), ip))


def describe(result: TapResult) -> str:
    line = f"{result.packet!s}  ->  {result.reply or 'ERROR'}"
    details = [f"from {result.sent_from}" if result.sent_from else "", f"{result.millis} ms"]
    if result.note:
        details.append(result.note)
    if result.error:
        details.append(result.error)
    return line + "   (" + ", ".join(d for d in details if d) + ")"


# ------------------------------------------------------------------------------ command line


def run_cli(args: argparse.Namespace) -> int:
    settings = Settings.load()
    server = args.server or settings.server
    port = args.port or settings.port
    if args.list:
        for d in settings.devices or []:
            print(f"{d.name:10} ID={d.id:8} TYPE={d.type:7} from={d.source_ip or '-'}")
        return 0
    name, raw_uid = args.tap
    device = next((d for d in settings.devices or [] if d.name.lower() == name.lower()), None)
    if device is None:
        print(f"Unknown device {name!r}; see --list", file=sys.stderr)
        return 2
    result = send_tap(server, port, device, clean_uid(raw_uid), fallback=not args.no_fallback)
    print(describe(result))
    return 0 if result.reply and not result.error else 1


# ------------------------------------------------------------------------------------ window


def run_gui() -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    settings = Settings.load()
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("1000x640")
    root.minsize(820, 520)
    results: queue.Queue[tuple[Device, TapResult]] = queue.Queue()

    # --- server row
    top = ttk.Frame(root, padding=(10, 10, 10, 4))
    top.pack(fill="x")
    ttk.Label(top, text="Server").pack(side="left")
    server_var = tk.StringVar(value=settings.server)
    ttk.Entry(top, textvariable=server_var, width=18).pack(side="left", padx=(4, 10))
    ttk.Label(top, text="Port").pack(side="left")
    port_var = tk.StringVar(value=str(settings.port))
    ttk.Entry(top, textvariable=port_var, width=6).pack(side="left", padx=(4, 10))
    fallback_var = tk.BooleanVar(value=settings.fallback)
    ttk.Checkbutton(
        top, text="Use this PC's IP when a device's IP is missing", variable=fallback_var
    ).pack(side="left", padx=(0, 10))
    ips_var = tk.StringVar()
    ttk.Label(top, textvariable=ips_var, foreground="#555").pack(side="right")

    def refresh_ips():
        ips_var.set("This PC: " + (", ".join(local_ips()) or "no IPv4 address"))

    refresh_ips()

    body = ttk.PanedWindow(root, orient="horizontal")
    body.pack(fill="both", expand=True, padx=10, pady=4)

    # --- device list
    left = ttk.Frame(body)
    body.add(left, weight=3)
    columns = ("name", "id", "type", "ip")
    tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse", height=16)
    for col, title, width in [
        ("name", "Device", 90),
        ("id", "ID", 80),
        ("type", "TYPE", 70),
        ("ip", "Send from IP", 120),
    ]:
        tree.heading(col, text=title)
        tree.column(col, width=width, anchor="w")
    tree.pack(fill="both", expand=True)

    def fill_tree(select: str | None = None):
        tree.delete(*tree.get_children())
        for i, d in enumerate(settings.devices or []):
            tree.insert("", "end", iid=str(i), values=(d.name, d.id, d.type, d.source_ip or "-"))
        if select is not None and tree.exists(select):
            tree.selection_set(select)
        elif tree.get_children():
            tree.selection_set(tree.get_children()[0])

    def selected() -> Device | None:
        sel = tree.selection()
        return (settings.devices or [])[int(sel[0])] if sel else None

    buttons = ttk.Frame(left)
    buttons.pack(fill="x", pady=(6, 0))

    def edit_device(device: Device | None):
        """Add (device=None) or edit a device in a small dialog."""
        dialog = tk.Toplevel(root)
        dialog.title("Device")
        dialog.transient(root)
        dialog.grab_set()
        fields = {
            "name": tk.StringVar(value=device.name if device else ""),
            "id": tk.StringVar(value=device.id if device else ""),
            "type": tk.StringVar(value=device.type if device else "Input"),
            "source_ip": tk.StringVar(value=device.source_ip if device else ""),
            "template": tk.StringVar(value=device.template if device else ""),
        }
        rows = [
            ("Name", "name", "e.g. Door1-1"),
            ("ID it sends", "id", "e.g. Door1"),
            ("TYPE", "type", "Input / Output (doors), Pay (till), Master (card assign)"),
            ("Send from IP", "source_ip", "empty = this PC's IP"),
            ("Packet", "template", "empty = default; {id} {type} {uid}"),
        ]
        for r, (label, key, hint) in enumerate(rows):
            ttk.Label(dialog, text=label).grid(row=r, column=0, sticky="w", padx=10, pady=4)
            if key == "type":
                widget = ttk.Combobox(dialog, textvariable=fields[key], values=TYPES, width=28)
            else:
                widget = ttk.Entry(dialog, textvariable=fields[key], width=30)
            widget.grid(row=r, column=1, padx=10, pady=4)
            ttk.Label(dialog, text=hint, foreground="#666").grid(row=r, column=2, sticky="w")

        def save():
            values = {k: v.get().strip() for k, v in fields.items()}
            if not values["name"] or not values["id"]:
                messagebox.showerror(APP_NAME, "Name and ID are required.", parent=dialog)
                return
            new = Device(**values)
            devices = settings.devices or []
            if device is None:
                devices.append(new)
                index = len(devices) - 1
            else:
                index = devices.index(device)
                devices[index] = new
            settings.devices = devices
            settings.save()
            fill_tree(str(index))
            dialog.destroy()

        bar = ttk.Frame(dialog, padding=10)
        bar.grid(row=len(rows), column=0, columnspan=3, sticky="e")
        ttk.Button(bar, text="Save", command=save).pack(side="right")
        ttk.Button(bar, text="Cancel", command=dialog.destroy).pack(side="right", padx=6)

    def delete_device():
        device = selected()
        if device and messagebox.askyesno(APP_NAME, f"Delete {device.name}?"):
            settings.devices.remove(device)
            settings.save()
            fill_tree()

    def reset_devices():
        if messagebox.askyesno(APP_NAME, "Replace the list with the preset devices?"):
            settings.devices = preset_devices()
            settings.save()
            fill_tree()

    ttk.Button(buttons, text="Add", command=lambda: edit_device(None)).pack(side="left")
    ttk.Button(buttons, text="Edit", command=lambda: selected() and edit_device(selected())).pack(
        side="left", padx=4
    )
    ttk.Button(buttons, text="Delete", command=delete_device).pack(side="left")
    ttk.Button(buttons, text="Reset to preset", command=reset_devices).pack(side="right")

    # --- tap panel
    right = ttk.Frame(body, padding=(12, 0, 0, 0))
    body.add(right, weight=2)
    ttk.Label(right, text="Tap a card", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    device_var = tk.StringVar()
    ttk.Label(right, textvariable=device_var, foreground="#333").pack(anchor="w", pady=(4, 8))
    ttk.Label(right, text="Card UID").pack(anchor="w")
    uid_var = tk.StringVar(value=(settings.uids or [""])[0])
    uid_box = ttk.Combobox(right, textvariable=uid_var, values=settings.uids or [], width=26)
    uid_box.pack(anchor="w", pady=(2, 8))
    packet_var = tk.StringVar()
    ttk.Label(right, text="Packet").pack(anchor="w")
    ttk.Label(right, textvariable=packet_var, font=("Consolas", 10), wraplength=360).pack(
        anchor="w", pady=(2, 10)
    )
    tap_button = ttk.Button(right, text="Tap  (Enter)")
    tap_button.pack(anchor="w")
    reply_var = tk.StringVar(value="")
    reply_label = tk.Label(right, textvariable=reply_var, font=("Segoe UI", 20, "bold"))
    reply_label.pack(anchor="w", pady=(14, 0))
    reply_detail = tk.StringVar(value="")
    ttk.Label(right, textvariable=reply_detail, foreground="#555", wraplength=360).pack(anchor="w")

    def update_preview(*_):
        device = selected()
        uid = clean_uid(uid_var.get()) or "<UID>"
        if device:
            ip = device.source_ip or "this PC's IP"
            device_var.set(f"{device.name}: ID {device.id}, TYPE {device.type}, from {ip}")
            packet_var.set(f"${device.packet(uid)}$")
        else:
            device_var.set("Pick a device on the left.")
            packet_var.set("")

    tree.bind("<<TreeviewSelect>>", update_preview)
    uid_var.trace_add("write", update_preview)

    # --- log
    log_frame = ttk.Frame(root, padding=(10, 4, 10, 10))
    log_frame.pack(fill="both", expand=False)
    log_head = ttk.Frame(log_frame)
    log_head.pack(fill="x")
    ttk.Label(log_head, text="Log").pack(side="left")
    log = tk.Text(log_frame, height=10, font=("Consolas", 9), state="disabled", wrap="none")
    log.pack(fill="both", expand=True)
    colours = {"CARD_OK": "#1a7f37", "CARD_NO": "#b35900", "CARD_DENIED": "#c62828"}
    for tag, colour in colours.items():
        log.tag_configure(tag, foreground=colour)
    log.tag_configure("ERROR", foreground="#c62828")

    def write_log(text: str, tag: str = ""):
        log.configure(state="normal")
        log.insert("end", time.strftime("%H:%M:%S  ") + text + "\n", tag)
        log.see("end")
        log.configure(state="disabled")

    def clear_log():
        log.configure(state="normal")
        log.delete("1.0", "end")
        log.configure(state="disabled")

    ttk.Button(log_head, text="Clear", command=clear_log).pack(side="right")

    # --- tapping (network work runs in a thread so the window stays responsive)
    def tap(*_):
        device = selected()
        uid = clean_uid(uid_var.get())
        if device is None or not uid:
            messagebox.showinfo(APP_NAME, "Pick a device and type a card UID.")
            return
        try:
            port = int(port_var.get())
        except ValueError:
            messagebox.showerror(APP_NAME, "The port must be a number.")
            return
        settings.server, settings.port = server_var.get().strip(), port
        settings.fallback = fallback_var.get()
        settings.remember_uid(uid)
        settings.save()
        uid_box.configure(values=settings.uids)
        tap_button.state(["disabled"])
        reply_var.set("…")
        reply_label.configure(fg="#555")
        args = (settings.server, port, device, uid, settings.fallback)
        threading.Thread(target=lambda: results.put((device, send_tap(*args))), daemon=True).start()

    def poll_results():
        try:
            while True:
                device, result = results.get_nowait()
                tag = result.reply if result.reply in colours else ("ERROR" if result.error else "")
                write_log(f"{device.name:9} {describe(result)}", tag)
                reply_var.set(result.reply or "No answer")
                reply_label.configure(fg=colours.get(result.reply, "#c62828"))
                reply_detail.set(
                    "; ".join(
                        x
                        for x in (
                            f"sent from {result.sent_from}" if result.sent_from else "",
                            result.note,
                            result.error,
                        )
                        if x
                    )
                )
                tap_button.state(["!disabled"])
        except queue.Empty:
            pass
        root.after(100, poll_results)

    tap_button.configure(command=tap)
    root.bind("<Return>", tap)
    fill_tree()
    update_preview()
    poll_results()
    write_log(f"Settings: {settings_path()}")
    root.mainloop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--tap", nargs=2, metavar=("DEVICE", "UID"), help="tap once and exit")
    parser.add_argument("--list", action="store_true", help="list the saved devices and exit")
    parser.add_argument("--server", help=f"server address (default: saved, {DEFAULT_SERVER})")
    parser.add_argument("--port", type=int, help=f"server port (default: saved, {DEFAULT_PORT})")
    parser.add_argument(
        "--no-fallback", action="store_true", help="fail when the device's IP is missing"
    )
    args = parser.parse_args(argv)
    if args.tap or args.list:
        return run_cli(args)
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
