# RFID Device Integration Guide

How RFID devices talk to the backend. Three kinds of devices connect over the network:

| Purpose | What it is | A scan means |
|---|---|---|
| `ATTENDANCE` | Door unit of a building; a door has several units sharing one ID (`Door1` = Building 1) | The developer came in or went out (attendance) |
| `TILL` | Till reader **plugged into a seller's PC** (can be moved to another PC) | The developer is paying for the purchase that uses this reader |
| `ENROLL` | Card assign reader (`Master1`, `Master2`) used by staff | A card is read to register it or assign it in the web app |

**The real devices speak only raw TCP** (port 9100, section *Devices over raw TCP*). The HTTP
endpoints below (scan, batch, heartbeat with an API key or a door's fixed IP) remain for
programs that can use HTTP.

---

## 1. Registration and API keys

A manager (permission `rfid.device.manage`) registers each device in the admin UI or API:

```http
POST /api/v1/rfid/devices/
{"code": "READER-001", "name": "Main entrance", "location": "Lobby", "purpose": "ATTENDANCE",
 "direction": "IN"}

POST /api/v1/rfid/devices/
{"code": "Door1", "name": "Door1-1", "building": 1, "allowed_ip": "192.168.100.151"}

POST /api/v1/rfid/devices/
{"code": "Reader1", "name": "Till reader 1", "purpose": "TILL"}

POST /api/v1/rfid/devices/
{"code": "Master1", "name": "Front desk", "purpose": "ENROLL"}
```

- `code` is the **ID the device sends**. Tills and card assign readers: each ID once. Door
  units of one door share the ID; each unit is its own device with its own `allowed_ip`
  (and usually its own `name`, e.g. `Door1-1`).

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

### Devices over raw TCP

All the devices (doors, till readers, card assign readers) open a **TCP connection to port
9100** of the server and send one packet per tap, framed by `$` at the start and the end:

```
$ID:Door1,TYPE:Input,UID=DC62B3E3$       door unit, way in
$ID:Door1,TYPE:Output,UID=DC62B3E3$      door unit, way out
$ID:Reader1,TYPE:Pay,UID=DC62B3E3$       till reader (ID:ID:Reader1 is accepted too)
$ID:Master1,TYPE:Master,UID=DC62B3E3$    card assign reader
```

Fields are `key:value` or `key=value`, separated by commas, keys case-insensitive. The
server **answers every packet** with one line ending in `\r\n`:

| Device | `CARD_OK` | `CARD_NO` | `CARD_DENIED` |
|---|---|---|---|
| Door | card accepted (also a repeated tap) | card not registered | registered but not assigned, blocked, retired, or developer suspended / terminated |
| Till | card can pay (assigned to an active developer) | anything else | — |
| Card assign | card already registered | new card (registered by this tap) | — |

A packet the server can't use (unknown ID or IP, `TYPE` not fitting the device, bad UID)
gets `CARD_NO`, and the reason is logged (`journalctl -u backend-tcp`: `rejected
ID='Door1' from 192.168.100.155 (no such device/IP)` or `invalid frame from ...`).

- **Identification, no keys:** a door unit by `ID` **and** the connection's address, which
  must be the unit's `allowed_ip` (several units share an ID, each with its own IP). Till
  and card assign readers by `ID` alone, from any address. There is no proxy in front of
  this port, so the peer address is the real sender. Put the devices on their own network
  segment and firewall port 9100 to it.
- `TYPE` must fit the device: doors `Input`/`Output` (also `in`/`out`), tills `Pay`, card
  assign readers `Master`.
- One connection can carry one packet or many (it may stay open). Bytes between packets
  (line breaks, spaces) are ignored. A packet may arrive split across several TCP reads.
- Limits: packets up to 2 KB; a connection idle for 5 minutes is closed (just reconnect);
  at most 10 connections per IP.
- The scans then follow the same path as HTTP scans: attendance, occupancy, the live
  dashboard, till payments, the card assign page.
- **Online status:** a device counts as online for 2 minutes after its last packet (TCP
  devices send no heartbeat).
- **JSON packets** are still accepted on this port for other programs:
  `${"ID":"Door1","Type":"in","UID":"04A2B3C4"}$` is answered with
  `${"result":"ACCEPTED","accepted":true,"direction":"IN","message":"Welcome, Ada Lovelace","event_id":311}$`
  (or `"result":"ERROR"` with an `error`); the same identification rules apply.

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

**Till readers** send their taps over TCP (`$ID:Reader1,TYPE:Pay,UID=...$`, section 1) and
are found by their `ID` alone. A program that uses HTTP instead needs the reader's API key
(`Authorization: Device <key>`) and sends:

```http
POST /api/v1/rfid/events/
Authorization: Device <key>
Content-Type: application/json

{"ID": "Reader1", "TYPE": "pay", "UID": "04A2B3C4"}
```

- `TYPE` must fit the device: till readers send `pay`, doors send `in`/`out`.

- Each till reader is **assigned to a seller**; only that seller's purchases use it.
- The server attaches an accepted tap only to the purchase **waiting for a card** on this
  reader (the seller pressed **Scan card to buy** in the last 2 minutes) and returns its id in
  `purchase`. Old unpaid drafts never get taps. If no purchase is waiting, `purchase` is
  `null` and the message says "No purchase is waiting for a card"; over TCP the reader gets
  `CARD_NO`. The tap ends the wait.
- The seller's browser shows who tapped (`presented_card` on the purchase) and asks the
  developer for their PIN. **The browser never sends the card number**, so it must come
  from this program.
- A tap is valid for **2 minutes**. To take another card, the seller scans again.
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
