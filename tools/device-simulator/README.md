# RFID device simulator (Windows)

A small window that behaves like the real RFID devices: pick a device, type a card UID,
press **Tap**. It opens a TCP connection to the server (port 9100), sends exactly the packet
the device sends and shows the answer:

| Device | Packet | Answers |
|---|---|---|
| Door unit, way in | `$ID:Door1,TYPE:Input,UID=DC62B3E3$` | `CARD_OK`, `CARD_NO`, `CARD_DENIED` |
| Door unit, way out | `$ID:Door1,TYPE:Output,UID=DC62B3E3$` | `CARD_OK`, `CARD_NO`, `CARD_DENIED` |
| Till reader | `$ID:Reader1,TYPE:Pay,UID=DC62B3E3$` | `CARD_OK`, `CARD_NO` |
| Card assign reader | `$ID:Master1,TYPE:Master,UID=DC62B3E3$` | `CARD_OK`, `CARD_NO` |

The list starts with the company's devices: Master1–2, Reader1–2, Door1-1…4
(192.168.100.151–154) and Door2-1…6 (192.168.100.201–206). Add, edit or delete devices in the
window; the list, the server address and the last card UIDs are saved in
`%APPDATA%\DeviceSimulator\settings.json`.

## Run it

1. Install Python 3.12 from python.org (keep **tcl/tk and IDLE** ticked: that is the window
   toolkit). The Python in the offline kit has no window toolkit.
2. Copy `device_simulator.py` to the PC and double-click it, or run
   `py device_simulator.py`.
3. Enter the **server address** (the server's network IP, e.g. `192.168.100.10`, not
   `localhost`) and port `9100`.

Command line, without the window:

```
py device_simulator.py --list
py device_simulator.py --tap Door1-1 DC62B3E3 --server 192.168.100.10
```

## Door units: send from their own IP

The server recognises a door unit by its ID **and the address it sends from** (Door1-1 must
come from 192.168.100.151). The simulator sends each device's packets from its **Send from
IP** when this PC has that address. Add the door addresses to the PC's network card once, in
an administrator command prompt (replace `Ethernet` with the adapter name from `ipconfig`):

```
netsh interface ipv4 add address "Ethernet" 192.168.100.151 255.255.255.0
netsh interface ipv4 add address "Ethernet" 192.168.100.152 255.255.255.0
rem ... and so on for .153, .154, .201 - .206
```

Remove one again with `netsh interface ipv4 delete address "Ethernet" 192.168.100.151`. The
addresses must not be used by real devices on the same network at the same time.

If the PC doesn't have a device's address, the simulator sends from the PC's own address and
says so in the log (switch this off with the checkbox to get an error instead). The server
then answers `CARD_NO` unless a door unit is registered with the PC's address. Till readers
and card assign readers are recognised by their ID alone, from any address.

The server must know the devices: register them in the web app under **Readers → New
device** (doors: code `Door1`, name `Door1-1`, building, IP `192.168.100.151`; tills:
`Reader1`; card assign readers: `Master1`). Unregistered devices always get `CARD_NO`; the
server log (`journalctl -u backend-tcp` or `logs\mgmt-tcp.out.log` on Windows) says why.

## Make an .exe (optional)

On a Windows PC with internet:

```
py -m pip install pyinstaller
py -m PyInstaller --onefile --windowed --name DeviceSimulator device_simulator.py
```

The program is `dist\DeviceSimulator.exe`; it runs on PCs without Python.
