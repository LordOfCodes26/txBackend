# Card reader bridge (Windows, offline)

Connects the card readers on USB (Master = card assign reader, Pay = till reader) to the
server's RFID TCP listener (port 9100). For every card tap it reads the reader's packet
(`$ID:Reader1,TYPE:Pay,UID=04DF0000E6$`), sends it to the server on a new connection and
writes the answer (`CARD_OK`, `CARD_NO`, `CARD_DENIED`) back to that reader.

It runs as a Windows service and needs nothing from the people using the PC:

- It starts with Windows, before anyone logs in, and restarts itself if it stops.
- It listens on **every CH341 USB adapter** (the readers' chip) and checks for new ones
  every 3 seconds. A reader works in any USB socket, whatever COM number Windows gives it;
  unplug and plug back in, another socket, a reboot: nothing to do. Other serial devices
  (receipt printers, scales) are left alone.
- The CH341 USB driver comes with the kit: the PC doesn't need Windows Update.
- Several readers on one PC are fine: each packet says which reader sent it (`ID:Master1`).
- No Python and no internet needed on the PC.

## Install (once per PC)

1. Copy the kit's .zip to the PC (USB stick), right-click it → **Extract All...**
2. In the extracted folder, double-click **`install.bat`** and click **Yes** when Windows
   asks for permission.
3. Type the server's IP address (or press **Enter** for the one shown), then press
   **Enter** again for TCP port **9100** (or type the server's port if it was changed with
   `sudo mgmt change-door-port`). `192.168.1.10:9200` in one go works too.
4. It checks that the server answers, installs the USB driver and the service and says
   **Done**. Plug in the readers; that's all. (If it asks for a restart, restart the PC.)

If it says **Installation FAILED**, the line above it says why.

**Update:** unzip the new kit and double-click `install.bat` again.
**Remove:** double-click `uninstall.bat` (also in `C:\ReaderBridge`).

The programs go to `C:\ReaderBridge`; the service is "Card reader bridge" in `services.msc`.

## Files

| File | |
|---|---|
| `ReaderBridge.exe` | the bridge |
| `nssm.exe` | runs the bridge as a service ([NSSM](https://nssm.cc), public domain) |
| `install.bat` / `uninstall.bat` | install or remove the service |
| `driver\` | WCH CH341 USB driver for Windows 10/11 (version in `CH341SER.INF`) |
| `server.txt` | the server address (`IP` or `IP:PORT`) `install.bat` offers, when the kit was built with one |
| `SHA256SUMS.txt` | checksums of the two programs |

## For the administrator

**Log:** `C:\ReaderBridge\bridge.log`. Each tap is a `->` line (packet) and a `<-` line
(answer), with the COM port:

```
2026-10-23 09:12:01 INFO COM5 open
2026-10-23 09:14:30 INFO COM5 -> $ID:Reader1,TYPE:Pay,UID=04DF0000E6$
2026-10-23 09:14:30 INFO COM5 <- CARD_OK
```

**Another device with a CH341 chip on the PC** (some cheap printers and scales have one):
the bridge would open its port too and the other program could not use it. Keep that port
out:

```
install.bat 192.168.1.10 9100 115200 "--exclude COM7"
```

That only works while the device keeps its COM number: fix it in Device Manager → Ports →
the device → Properties → Port Settings → Advanced → COM Port Number.

**Other options** (from a command prompt in the kit folder):

```
install.bat SERVER [TCPPORT] [BAUD] [EXTRA]       defaults: 9100, 115200
ReaderBridge.exe --list-ports                     COM ports; CH341 ones marked "reader"
install.bat 192.168.1.10 9100 115200 --any-usb      readers with another USB chip
ReaderBridge.exe --server 192.168.1.10:9100 --check   does the server answer on that port?
ReaderBridge.exe --server 192.168.1.10:9100       run in the window (stop the service first)
```

**Problems**

- **Tapping does nothing (no `->` line):** look for `ignored (not a $...$ packet)` lines
  in the log. A few odd bytes per tap (like `b'\x10\x90'`) mean a **wrong baud rate**:
  the readers use 115200 (the default); for another rate reinstall with it, e.g.
  `install.bat 192.168.1.10 9100 9600`. Readable text
  without `$` means the reader sends another format. To see every byte, stop the service
  (`net stop ReaderBridge`), then in a command prompt in `C:\ReaderBridge` run
  `ReaderBridge.exe --server 192.168.1.10:9100 --debug`, tap a card, and read the
  `raw` lines; Ctrl+C and `net start ReaderBridge` afterwards.
- **Reader plugged in but nothing in the log:** Device Manager → Ports should show
  "USB-SERIAL CH340 (COM5)". A yellow mark or an "Unknown device" means no driver: run
  `install.bat` again; it installs the driver from the kit.
- **`no reply from server, answering CARD_NO`:** the PC can't reach the server: check
  `ping <server IP>` and the server's listener (`journalctl -u backend-tcp`).
- **Reader always gets `CARD_NO`:** register it in the web app under **Readers → New
  device** (`Master1`, `Reader1`, ...); the TCP log page says why each packet was refused.
- **A port keeps closing in the log:** Power Options → sleep **Never**, and Device Manager →
  each *USB Root Hub* → Power Management → untick *Allow the computer to turn off this
  device*.

## Build (on the Linux server)

```
SERVER_IP=192.168.1.10 tools/reader-bridge/build_kit.sh 2026.10.23
SERVER_IP=192.168.1.10:9200 tools/reader-bridge/build_kit.sh 2026.10.23   other port
```
