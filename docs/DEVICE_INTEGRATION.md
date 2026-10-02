# RFID Device Integration Guide

How RFID devices talk to the backend. Two kinds of devices connect over the network:

| Purpose | What it is | A scan means |
|---|---|---|
| `ATTENDANCE` | Door device of a building (`Door1` = Building 1, `Door2` = Building 2) | The developer came in or went out (attendance) |
| `TILL` | Till reader **plugged into a seller's PC** (can be moved to another PC) | The developer is paying for the purchase that uses this reader |

Both use the same authentication, scan endpoint and heartbeat. Differences are called out
below.

---

## 1. Registration and API keys

A manager (permission `rfid.device.manage`) registers each device in the admin UI or API:

```http
POST /api/v1/rfid/devices/
{"code": "READER-001", "name": "Main entrance", "location": "Lobby", "purpose": "ATTENDANCE",
 "direction": "IN"}

POST /api/v1/rfid/devices/
{"code": "Reader1", "name": "Till reader 1", "purpose": "TILL", "sn": "ZK2024A0001234"}
```

- The response contains `api_key` **once**. Store it in the device's or program's
  configuration file, readable only by the program's user account.
- A `TILL` reader is **not tied to a counter or seller**: it's plugged into a seller's PC
  and may be moved. The seller's screen chooses which reader it uses, and each purchase
  records that reader.
- A lost or leaked key: `POST /api/v1/rfid/devices/{id}/rotate-key/` returns a new key, and
  the old one stops working immediately.
- To retire a device: `PATCH /api/v1/rfid/devices/{id}/ {"is_active": false}`.

### Doors without API keys: fixed-IP authentication

The building doors can't send an `Authorization` header, so they are authenticated by
their **fixed IP address** instead:

- In the device settings, set `allowed_ip` on each door (e.g. `PATCH
  /api/v1/rfid/devices/{id}/ {"allowed_ip": "10.20.0.11"}`). This is audited.
- A request **without** an `Authorization` header is accepted only when it comes from that
  IP **and** its body names the device: `{"ID": "Door1", ...}`. Door2's IP can't post as
  Door1. Several doors behind one controller may share an IP, since `ID` tells them apart.
- This works for the scan, batch (`{"ID": "Door1", "events": [...]}`) and heartbeat
  (`{"ID": "Door1"}`) endpoints. Only `ATTENDANCE` devices may use it; till programs always
  need their key. A device that also has a key can keep using it.
- The server takes the IP from nginx, which overwrites any client-sent
  `X-Forwarded-For` / `X-Real-IP`, so faking these headers doesn't work.
- **Network requirement:** anyone who can take over a door's IP on your network could post
  scans as that door. Put the doors on their own network segment (VLAN) or firewall the
  door IPs so only the door devices can use them, and give them static/reserved addresses
  (DHCP reservations).

Refused requests get `401 AUTHENTICATION_FAILED` ("No door device with this ID is
registered for this IP").

### Doors over raw TCP (`$`-framed JSON)

The building doors don't speak HTTP. They open a **TCP connection to port 9100** of the
server and send JSON packets framed by `$` at the start and the end:

```
${"ID":"Door1","Type":"in","UID":"04A2B3C4"}$
```

- One connection can carry one packet or many (it may stay open). Bytes between packets
  (line breaks, spaces) are ignored. A packet may arrive split across several TCP reads.
- The server **answers every packet** in the same framing:

  ```
  ${"result":"ACCEPTED","accepted":true,"direction":"IN","message":"Welcome, Ada Lovelace","event_id":311}$
  ${"result":"ERROR","accepted":false,"error":"No door device with this ID is registered for this IP."}$
  ```

  `result` is one of the scan results in section 4 (`ACCEPTED`, `DUPLICATE`, `UNKNOWN_CARD`,
  …), or `ERROR` when the packet itself was rejected. Use `accepted` for the green/red
  light and `message` for a display. A door that ignores replies still works.
- **Authentication is the fixed IP** (same rule as above): the connection must come from
  the `allowed_ip` registered for the device named in `ID`. This port is for the doors
  only; till readers use HTTPS (see *TILL devices only*).
- **Finding a door's IP:** point the door at the server and tap a card. The rejection is
  logged with the address the server saw (`journalctl -u backend-tcp`: `rejected
  ID='Door1' from 203.0.113.5`). Register that address as the door's `allowed_ip`. If the
  doors reach the server through a router/NAT, they all appear with the router's address,
  which is fine because `ID` tells them apart.
- Limits: packets up to 2 KB; a connection idle for 5 minutes is closed (just reconnect);
  at most 10 connections per IP. Optional fields `event_time` and `client_event_id`
  (section 4) work here too.
- The scans then follow the same path as HTTP scans: attendance, occupancy and the live
  dashboard feed.

---

## 2. Connection basics

| | |
|---|---|
| Base URL | `https://<server>/api/v1/` (HTTPS) |
| Authentication | Header `Authorization: Device <api_key>` on every request (doors: fixed IP, see section 1) |
| Body | JSON, `Content-Type: application/json` |
| Times | ISO 8601 **with timezone**, e.g. `2026-10-01T08:59:58Z` or `...+09:00` |
| Timeout | Use 5 seconds per request |
| Language | `message` / `display_message` / `error` texts use the server setting `DEVICE_LANGUAGE`: `en` (default) or `ko-kp` (Korean, UTF-8). Use Korean only if the reader's screen has a Hangul font |

**TLS:** the server may use a certificate from your company's internal certificate
authority (it's an offline network). Install that CA certificate on the device or
computer. **Do not turn off certificate verification**, because the API key would then be
exposed to anyone who can intercept the traffic.

**Errors** always look like this:

```json
{"error": {"code": "VALIDATION_ERROR", "message": "Invalid input.", "details": {"uid": ["..."]}}}
```

| HTTP | Meaning | What the device should do |
|---|---|---|
| 200 / 201 | OK | Show the result (section 4) |
| 400 | Bad request (e.g. clock far in the future) | Log it; don't retry unchanged |
| 401 | Wrong, rotated or deactivated key | Show "Device not authorised", stop retrying, alert an admin |
| 5xx, timeout, no network | Server or network problem | Retry (section 5) |

---

## 3. Heartbeat

Every device sends a heartbeat every **30 seconds** (the server says how often in
`heartbeat_seconds`), including right after starting up.

```http
POST /api/v1/rfid/device/heartbeat/
Authorization: Device <api_key>

{"app_version": "till-agent 1.2.0"}
```

Response:

```json
{
  "code": "Reader1", "name": "Till reader 1", "location": "", "purpose": "TILL",
  "direction": "BOTH",
  "server_time": "2026-10-01T09:00:00.123456+00:00",
  "heartbeat_seconds": 30, "debounce_seconds": 10, "max_future_skew_seconds": 300
}
```

- The admin UI shows a device as **online** when it was heard from within the last 2 minutes
  (`GET /api/v1/rfid/devices/?online=false` lists offline devices).
- **Clock:** compare `server_time` with the local clock. If they differ by more than a few
  seconds, correct the time you send with scans by that offset, or better, sync the device
  to a local time server (NTP). Scans timestamped more than `max_future_skew_seconds` in the
  future are rejected.
- Use the response to display the configuration (e.g. "Reader1"), so staff can see the
  device is set up correctly.

---

## 4. Sending a scan

```http
POST /api/v1/rfid/events/
Authorization: Device <api_key>

{"uid": "04:A2:B3:C4", "event_time": "2026-10-01T08:59:58Z", "client_event_id": "Reader1-000123"}
```

| Field | Required | Notes |
|---|---|---|
| `uid` | yes | Card UID in hex. `:`, `-` and spaces are ignored, and case doesn't matter |
| `type` | doors / tills | Doors: `in` or `out` (used for attendance). Till readers: `pay`. Also accepted as `Type`/`TYPE` |
| `event_time` | recommended | When the card was read (device clock). Default: time received |
| `client_event_id` | strongly recommended | Unique per scan, 1–64 chars. Makes retries safe (section 5) |
| `device_id` | no | If sent, must equal the device's `code` |

**Door device format.** The building doors send their own field names, which the server
accepts as-is (key names and values are case-insensitive):

```http
POST /api/v1/rfid/events/
Authorization: Device <api_key of Door1>

{"ID": "Door1", "Type": "in", "UID": "04A2B3C4"}
```

| Door field | Meaning | Server field |
|---|---|---|
| `ID` | `Door1` or `Door2`; must be the code of the device whose key is used | `device_id` |
| `Type` | `in` or `out` | `direction` |
| `UID` | Card UID | `uid` |

The reply's `display_message` is "Welcome, <name>" for `in` and "Goodbye, <name>" for `out`.
An `in` followed quickly by an `out` is two real movements; only the *same* direction
repeated within 10 seconds is marked `DUPLICATE`. Every door scan is also pushed live to the dashboards
(see the frontend guide, *Realtime attendance*). These scans also drive **occupancy** (who is inside which
building right now): a developer's latest scan decides where they are, so every door must
report `in`/`out` correctly. Each door device is linked to its building (`Door1` → Building 1,
`Door2` → Building 2) in the device settings.

Response (`201` new scan, `200` = this `client_event_id` was already received):

```json
{
  "id": 4711,
  "result": "ACCEPTED",
  "accepted": true,
  "direction": "",
  "display_message": "Ada Lovelace - enter PIN",
  "developer": {"id": 1, "employee_number": "E0001", "full_name": "Ada Lovelace", "department": "Engineering"},
  "purchase": 52,
  "event_time": "2026-10-01T08:59:58Z",
  "client_event_id": "Reader1-000123"
}
```

Show `display_message` on the screen, with a green light/beep when `accepted` is true and a
red one otherwise:

| `result` | `accepted` | Typical `display_message` |
|---|---|---|
| `ACCEPTED` | true | Attendance: "Welcome, Ada Lovelace". Till: "Ada Lovelace - enter PIN" |
| `DUPLICATE` | true | Attendance only: same card again within 10 s (counted once) |
| `UNKNOWN_CARD` | false | "Unknown card" (the UID is recorded so an admin can register the card) |
| `UNASSIGNED_CARD` | false | "Card not assigned" |
| `BLOCKED_CARD` | false | "Card blocked" |
| `RETIRED_CARD` | false | "Card no longer valid" |
| `INACTIVE_DEVELOPER` | false | "Not active - contact your manager" |

A response with `accepted: false` is still a **successful request**; don't retry it.

### TILL devices only

**Till reader format.** Till readers (`Reader1`, `Reader2`, …, one per counter) send their
serial number with every tap. **No API key is needed**: `SN` + `ID` identify and
authenticate the reader.

```http
POST /api/v1/rfid/events/
Content-Type: application/json

{"SN": "ZK2024A0001234", "ID": "Reader1", "TYPE": "pay", "UID": "04A2B3C4"}
```

Till programs use **HTTPS only**; the TCP port 9100 is for the doors.

| Field | Meaning |
|---|---|
| `SN` | The reader's serial number, exactly as registered for this device (case and surrounding spaces don't matter) |
| `ID` | The reader's device code |
| `TYPE` | Always `pay` (case-insensitive) |
| `UID` | Card UID |

- Register each reader with its serial number: `POST /api/v1/rfid/devices/
  {"code": "Reader1", "purpose": "TILL", "sn": "ZK2024A0001234"}`.
  Each serial number can belong to only one device.
- Both `SN` and `ID` must match an active till reader, otherwise the reply is `401`
  ("Unknown till reader ID or serial number"). After **10 failed attempts from one
  address within 15 minutes**, that address is blocked for 15 minutes (guessing protection).
- `TYPE` must fit the device: till readers send `pay`, doors send `in`/`out`.
- Heartbeats work the same way: `POST /api/v1/rfid/device/heartbeat/ {"SN": "...", "ID": "Reader1"}`.
- **Security note:** a serial number is often printed on the device. Anyone who knows it
  and the reader's ID could send fake taps for that counter. Nobody can be charged without
  the developer's PIN, but treat serial numbers as confidential (keep labels covered, don't
  share device lists). An API key (`Authorization: Device <key>`) is still accepted as a
  stronger alternative.

- **Run the till program on the seller's PC itself, and send a heartbeat every 30 s.** The
  server links the reader to that PC by network address: the seller's browser on the same
  PC sees the reader as "connected" (heard from in the last 2 minutes from its address).
- The server attaches an accepted tap to the **newest open (DRAFT) purchase that uses this
  reader**, or else the newest open purchase created from the **same address** as the tap,
  and returns its id in `purchase`. If none is open,
  `purchase` is `null` and the message says "No open purchase for this reader". The seller
  should start the purchase first, then let the developer tap. Moving the reader to another
  PC needs no change here; that PC simply selects the reader.
- The seller's browser shows who tapped (`presented_card` on the purchase) and asks the
  developer for their PIN. **The browser never sends the card number**, so it must come
  from this program.
- A tap is valid for **2 minutes**. A newer tap replaces the previous one.
- Till taps are not debounced: paying twice in a row is normal.
- **Never buffer till taps.** If the server can't be reached, show "Offline - cannot pay
  right now" and discard the tap. A late tap would be ignored anyway.

### Live events for till programs

Till readers aren't tied to a counter, so they don't subscribe to a counter's live channel.
The result of every tap comes back in the reply to the program's own request (above). The
seller's screen receives the live events.

---

## 5. Retries and offline buffering (ATTENDANCE devices)

Attendance scans must never be lost, so a reader keeps a **local queue**:

1. On every card read, create the scan with `event_time = now` and a **new**
   `client_event_id` (e.g. `<device code>-<counter persisted on disk>`, or a UUID), and
   store it in the queue (on disk, so it survives restarts).
2. Send the oldest queued scan. On `200`/`201`, remove it from the queue. On a timeout,
   network error or `5xx`, keep it and retry with **the same `client_event_id`**, backing
   off 1 s, 2 s, 5 s, 10 s, then 30 s. The server stores each `client_event_id` only once,
   so a retry after a lost response is harmless.
3. When the queue holds more than a few scans (e.g. after an outage), upload them in batches:

```http
POST /api/v1/rfid/events/batch/
Authorization: Device <api_key>

{"events": [
  {"uid": "04A2B3C4", "event_time": "2026-10-01T07:58:01Z", "client_event_id": "READER-001-000981"},
  {"uid": "04FFEE01", "event_time": "2026-10-01T07:58:40Z", "client_event_id": "READER-001-000982"}
]}
```

- Up to 500 events per request, each with a unique `client_event_id`. `event_time` is
  required here, since the receive time would be wrong for buffered scans.
- The response lists every event: `{"client_event_id", "id", "result", "created"}`. Remove
  all of them from the queue. `created: false` means the server already had it from an
  earlier attempt.
- The server processes the batch oldest first, so attendance and duplicate detection come
  out right even if the queue order was mixed.
- `400` means the whole batch was rejected (e.g. a time far in the future). Fix or drop the
  offending event and resend.
- Only `ATTENDANCE` devices may use the batch endpoint.

While offline, show "Recorded offline" with a green light: the scan is safe in the queue.

---

## 6. Reference: till program loop (pseudo-code)

```python
import time, uuid, requests

API = "https://server/api/v1"
HEADERS = {"Authorization": f"Device {API_KEY}"}
VERIFY = "/etc/company-ca.pem"  # internal CA; never verify=False


def heartbeat():
    r = requests.post(
        f"{API}/rfid/device/heartbeat/",
        json={"app_version": "till-agent 1.0"},
        headers=HEADERS,
        timeout=5,
        verify=VERIFY,
    )
    r.raise_for_status()
    return r.json()


def on_card(uid):  # called by the reader driver for every tap
    scan = {"uid": uid, "event_time": utc_now_iso(), "client_event_id": str(uuid.uuid4())}
    for delay in (0, 1, 2):  # a few quick retries, then give up (no buffering)
        time.sleep(delay)
        try:
            r = requests.post(
                f"{API}/rfid/events/", json=scan, headers=HEADERS, timeout=5, verify=VERIFY
            )
        except requests.RequestException:
            continue
        if r.status_code == 401:
            return show("Device not authorised", ok=False)
        if r.status_code >= 500:
            continue
        body = r.json()
        return show(body.get("display_message", "Error"), ok=body.get("accepted", False))
    show("Offline - cannot pay right now", ok=False)
```

Run `heartbeat()` at startup and then every `heartbeat_seconds` on a background thread.
Show its `code` in the program's window, so staff can see which reader it is.

---

## 7. Testing with curl

```bash
KEY=...   # from device registration
curl -s -X POST https://server/api/v1/rfid/device/heartbeat/ \
  -H "Authorization: Device $KEY" -H "Content-Type: application/json" -d '{}'
curl -s -X POST https://server/api/v1/rfid/events/ \
  -H "Authorization: Device $KEY" -H "Content-Type: application/json" \
  -d '{"uid": "04DE000001", "client_event_id": "test-0001"}'
```

Interactive API docs: `https://server/api/docs/`. Under **Authorize → deviceKey**, enter
`Device <key>`.
