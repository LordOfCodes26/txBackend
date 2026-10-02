# Installing the backend on the offline server

This folder contains everything needed. The server does **not** need internet access.

| File | What it is |
|---|---|
| `backend-<version>.tar.gz` | The bundle: application, Python packages and Ubuntu packages |
| `backend-<version>.tar.gz.sha256` | Checksum, to detect a damaged copy |
| `prepare-server.sh` | Prepares a freshly installed Ubuntu (run once, before the installer) |
| `setup-dev.sh` | Optional: a development copy (code, tests, own database) for a user |
| `install-backend.sh` | The installer (installs and upgrades) |
| `README-INSTALL.md` | This guide |

---

## 1. Before you start

- **Server:** Ubuntu Server **24.04** LTS, **x86_64** (Intel/AMD), freshly installed.
  The installer stops on any other system.
- **Access:** an account that can use `sudo`.
- **Disk:** at least 10 GB free (application, database and its backups).
- **Network:** the server needs a **fixed IP address** in the company network. Clients
  reach it on port **443** (HTTPS, also 80); door devices send to TCP port **9100**.
  To see the server's IP, run on the server:

  ```bash
  hostname -I      # e.g. 192.168.1.10   (ignore 127.0.0.1 and 172.17.x.x)
  ip -br addr      # the same, per network card
  ```

  Use the address in your company network (often `192.168.x.x` or `10.x.x.x`). Make it
  fixed in Ubuntu's network settings (or reserve it in the router) before installing.
- **Decide before installing:**
  - the **server IP** (see above);
  - the company **timezone** (e.g. `Asia/Pyongyang`);
  - the **language**: English (`en`) or Korean (`ko`), for the web/API and for the
    reader screens (Korean on readers only if their screens can show Korean letters);
  - the **email of the first admin** account.

## 2. Copy the files

Copy all files of this folder onto the server, into one folder (USB disk or the internal
network), for example `/home/<you>/backend-install/`:

```bash
cd /home/<you>/backend-install
ls   # README-INSTALL.md  backend-<version>.tar.gz(.sha256)  install-backend.sh  prepare-server.sh  setup-dev.sh
```

## 3. Prepare the server (once, on a fresh Ubuntu)

```bash
sudo bash prepare-server.sh
```

It asks step by step (Enter keeps what is shown):

1. **checks** the system (Ubuntu 24.04, x86_64, disk space, memory);
2. **server name**, e.g. `backend-server`;
3. **timezone and clock**: the company's time server if there is one, otherwise it shows
   the clock and lets you correct it (attendance and purchases use this clock);
4. **fixed IP address**: network card, IP with prefix (e.g. `192.168.1.10/24`), gateway and
   DNS (`none` if the network has none);
5. **firewall**: opens SSH (22), web (80, 443) and the door port 9100 (optionally only
   for the doors' network, e.g. `192.168.1.0/24`);
6. switches off Ubuntu's automatic online updates (they can't work offline).

At the end it prints the install command to run next, with the IP and timezone filled in.

- Connected over SSH? When the IP changes, the script uses `netplan try`: press **Enter
  within 2 minutes** to keep the new address, then reconnect to it. If you don't, the old
  settings come back by themselves, so you can't lock yourself out. Safest is to run it at
  the server's own screen.
- See first what it would do, without changing anything: `sudo bash prepare-server.sh --dry-run`
- All answers can be given as options (`--help` lists them), e.g.
  `sudo bash prepare-server.sh --timezone Asia/Pyongyang --ip 192.168.1.10/24 --gateway 192.168.1.1 --dns none`

Already set up the network and firewall yourself? Skip those steps with
`--skip-network --skip-firewall`, or skip the script entirely.

## 4. Install

```bash
sudo bash install-backend.sh
```

It checks the bundle, unpacks it and installs everything (about 2–3 minutes). On a first
install it asks:

```
Continue installing <version>? (yes/no) [yes]:
Company timezone (e.g. Asia/Seoul, Europe/Berlin) [UTC]:
    this server's IP addresses: 192.168.1.10
Server IP address that computers and doors will use [192.168.1.10]:
Default language for the web and API: en = English, ko = Korean [en]:
Language on door/till reader screens (ko only if they show Korean letters): en / ko [en]:
Admin email [admin@example.com]:
Password: / Password (again):
```

Or give the answers up front:

```bash
sudo bash install-backend.sh --server-ip 192.168.1.10 --timezone Asia/Pyongyang \
     --language ko --device-language en --admin-email admin@chonha.com
```

| Option | Meaning |
|---|---|
| `--server-ip IP` | The server's IP in the company network (`hostname -I`). Used for the web address, the doors and the temporary certificate |
| `--timezone Area/City` | Company timezone |
| `--language en\|ko` | Web/API language when the browser doesn't choose one |
| `--device-language en\|ko` | Language on door and till reader screens |
| `--hosts "name1,name2"` | Extra host names / IPs clients use (the server's own are added) |
| `--admin-email EMAIL` | First admin account (asks for its password) |
| `--no-admin` | Don't create an admin account |
| `--yes` | Don't ask for confirmation |
| `--extract-only [DIR]` | Only check and unpack the bundle, don't install |

The installer sets up PostgreSQL, Redis, nginx, the backend services and daily backups,
creates a random secret key and database password, and creates all roles (ADMIN, BOSS,
MANAGER, FINANCE_MANAGER, BUILDING_MANAGER, BUILDING_OWNER, SELLER, DEVELOPER).

### Success looks like this

```
==> Health checks
    postgresql                 active
    ...
    /health/                   200
    /health/backup/            200
    door listener :9100        open

Installed <version> successfully.
  Web / API:     https://<server-ip>/
```

If a check fails, see **13. Problems**.

## 5. Check it works

From a computer in the company network, open `https://<server-ip>/admin/` and sign in
with the admin account. The browser warns about the certificate the first time: the
installer made a temporary one for the server IP (see step 7).

## 6. The API: addresses and documentation

| What | Address |
|---|---|
| **API** (all requests start here) | `https://<server-ip>/api/v1/` |
| **Interactive documentation** (every endpoint, its fields and answers) | `https://<server-ip>/api/docs/` |
| API description file (OpenAPI), e.g. for the frontend's types | `https://<server-ip>/api/schema/` |
| Live updates (WebSocket) | `wss://<server-ip>/ws/…` |
| Door devices (TCP) | `<server-ip>:9100` |
| The development copy's API, when running | `http://127.0.0.1:8000/api/v1/`, docs `http://127.0.0.1:8000/api/docs/` (no sign-in) |

### Open the documentation

1. Sign in at `https://<server-ip>/admin/` with an **admin** account (the first admin from
   the installation, or any ADMIN user marked as staff). The docs need you to be signed in.
2. Open `https://<server-ip>/api/docs/`: every endpoint, grouped (auth, developers, rfid,
   attendance, purchases, …), with what to send and what comes back.
3. **To try a request there:** open `POST /api/v1/auth/token/` → *Try it out* → put your
   email and password → *Execute* → copy the `access` value. Click **Authorize** at the top,
   paste it, *Authorize*. Now *Try it out* works on every endpoint. A token lasts 15 minutes.

### Use it from a program or the terminal

Every request (except signing in) sends the token: `Authorization: Bearer <access token>`.

```bash
curl -sk https://<server-ip>/api/v1/auth/token/ -H 'Content-Type: application/json' \
  -d '{"email": "admin@chonha.com", "password": "..."}'            # → {"access": "...", "refresh": "..."}
curl -sk https://<server-ip>/api/v1/developers/ -H "Authorization: Bearer <access>"
```

(The door section, step 1, has a small `api` helper that does this for you.)

### The main groups

| Path (after `/api/v1/`) | What |
|---|---|
| `auth/token/`, `auth/token/refresh/`, `auth/me/`, `auth/password/`, `auth/logout/` | Sign in, refresh, who am I, change password, sign out |
| `users/`, `roles/` | Users and their roles |
| `developers/` | Developers |
| `rfid/cards/`, `rfid/assignments/`, `rfid/devices/`, `rfid/buildings/`, `rfid/events/` | Cards, who has which card, door and till readers, buildings, every tap |
| `attendance/records/`, `attendance/daily/`, `attendance/occupancy/` | Attendance, daily summary, who is inside now |
| `finance/accounts/`, `finance/transactions/`, `finance/deposits/`, `finance/adjustments/` | Developers' money |
| `sellers/`, `service-positions/`, `goods/`, `inventory/movements/` | Stores, counters, goods, stock |
| `purchases/` | Till sales (and `purchases/performance/`: sales per counter) |
| `rentals/`, `bookings/` | Courts and bookings |
| `seller-finance/accounts/`, `…/transactions/`, `…/payouts/`, `…/adjustments/` | Stores' money and payouts |
| `stats/` | Company statistics (BOSS dashboard) |
| `audit-logs/` | Who changed what |
| `realtime/ticket/` | A ticket to open the live-updates connection |

### The written reference

On the server, in `/opt/backend/current/docs/` (and in `~/backend-dev/docs/`):

- `FRONTEND_README.md`: every endpoint with permissions, filters, errors and examples;
- `DEVICE_INTEGRATION.md`: doors and till readers;
- `BACKUP_AND_RESTORE.md`: backups and how to restore.

## 7. After installing

1. **Certificate:** put your company's certificate on the server, then reload nginx:

   ```bash
   sudo cp cert.pem /etc/backend/tls/cert.pem
   sudo cp key.pem  /etc/backend/tls/key.pem
   sudo systemctl reload nginx
   ```

2. **Logins for each role** (`admin@chonha.com`, `boss@chonha.com`, …):

   ```bash
   sudo -u backend bash -c 'set -a; . /etc/backend/backend.env; set +a; \
     cd /opt/backend/current && .venv/bin/python manage.py create_role_users --domain chonha.com'
   ```

   Each new user gets a random password that is **shown only once**: store them safely.
   Add `--ask-password` to type one password for all of them instead.

3. **Link the accounts that need it** (in the web app or the admin):
   a SELLER login to its store, BUILDING_OWNER / BUILDING_MANAGER logins to their
   buildings, DEVELOPER logins to a developer profile.

4. **Buildings, doors and tills:** see **8. Connecting the door devices** and
   **9. Connecting the till readers**, step by step.

5. **Backups to a second place:** backups are made every night into
   `/var/backups/backend`, on the same disk. Set a second disk or machine in
   `/etc/backend/backup.conf` (`OFFSITE_DIR=` or `OFFSITE_RSYNC=`).

6. **Firewall:** `prepare-server.sh` already opened 22, 80, 443 and 9100. To limit 9100
   to the doors later: `sudo ufw allow from <door ip> to any port 9100 proto tcp`, then
   `sudo ufw delete allow 9100/tcp`.

## 8. Connecting the door devices (attendance)

Right after installing, the door service (`backend-tcp`) already listens on **TCP port
9100**, and doors that send `in` / `out` are understood. But the server **rejects every
door until it is registered**: a door is accepted only when its **ID** (`Door1`, `Door2`)
is registered **and** its scans come from that door's **fixed IP address**.

Do these steps once, on the server (a terminal on the server itself).

### Step 1. Sign in from the terminal

Use the admin account from the installation. The sign-in lasts 15 minutes: if a later
command answers `token_not_valid`, run this step again.

```bash
SERVER=https://localhost
read -p "Admin email: " EMAIL; read -s -p "Password: " PASSWORD; echo
TOKEN=$(EMAIL="$EMAIL" PASSWORD="$PASSWORD" python3 -c \
  'import json, os; print(json.dumps({"email": os.environ["EMAIL"], "password": os.environ["PASSWORD"]}))' \
  | curl -sk "$SERVER/api/v1/auth/token/" -H 'Content-Type: application/json' -d @- \
  | python3 -c 'import sys, json; print(json.load(sys.stdin).get("access", ""))')
[ -n "$TOKEN" ] && echo "Signed in." || echo "Sign-in failed: check the email and password."
api() { curl -sk -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; echo; }
api "$SERVER/api/v1/auth/me/"          # shows your account: the sign-in worked
```

### Step 2. Create the buildings

```bash
api -X POST "$SERVER/api/v1/rfid/buildings/" -d '{"code": "B1", "name": "Building 1"}'
api -X POST "$SERVER/api/v1/rfid/buildings/" -d '{"code": "B2", "name": "Building 2"}'
api "$SERVER/api/v1/rfid/buildings/"   # note each building's "id"
```

(Buildings can also be created in the admin: `https://<server-ip>/admin/` → RFID → Buildings.)

### Step 3. Register the doors

One door device per door, with the building it belongs to (use the ids from step 2):

```bash
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Door1", "name": "Building 1 door", "building": 1}'
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Door2", "name": "Building 2 door", "building": 2}'
api "$SERVER/api/v1/rfid/devices/?purpose=ATTENDANCE"   # note each door's "id"
```

The `code` must be exactly the **ID the door sends** (`Door1`, `Door2`). The answer also
shows an `api_key`: doors don't need it (they are recognised by their IP), ignore it.

### Step 4. Point each door at the server

In each door device's own settings:

| Setting | Value |
|---|---|
| Server address | the server IP chosen during the install, e.g. `192.168.1.10` (shown at the end of the install; `grep SERVER_IP /etc/backend/backend.env`) |
| Port | `9100` |
| Protocol | TCP, JSON between `$` signs: `${"ID": "Door1", "Type": "in", "UID": "..."}$` |
| ID | `Door1` (or `Door2`), the same as the `code` in step 3 |

### Step 5. Find each door's IP address

Watch the door log and tap any card on the door:

```bash
sudo journalctl -u backend-tcp -f
```

A line like this appears (press Ctrl+C to stop watching):

```
RFID TCP: rejected ID='Door1' from 192.168.1.50 (no matching door/IP)
```

`192.168.1.50` is that door's IP. Nothing appears? The door doesn't reach the server: check
step 4, the network cable, and the firewall (step 7). Give each door a **fixed IP** in the
door's settings or the router, otherwise it stops working when its address changes.

### Step 6. Allow each door by its IP

Use the door's id from step 3 and the IP from step 5:

```bash
api -X PATCH "$SERVER/api/v1/rfid/devices/1/" -d '{"allowed_ip": "192.168.1.50"}'
api -X PATCH "$SERVER/api/v1/rfid/devices/2/" -d '{"allowed_ip": "192.168.1.51"}'
```

Tap a card again: the door now gets an answer from the server. With a card that isn't
registered yet the door shows **"Unknown card"**: the connection works.

### Step 7. Firewall (if the server has one)

Allow port 9100 only from the doors:

```bash
sudo ufw allow from 192.168.1.50 to any port 9100 proto tcp
sudo ufw allow from 192.168.1.51 to any port 9100 proto tcp
```

### Step 8. Register developers and their cards

A tap is counted only for a card assigned to a developer. For each developer (usually from
the web app; by command for a first test):

```bash
api -X POST "$SERVER/api/v1/developers/" -d '{"employee_number": "E001", "full_name": "Ada Kim", "building": 1}'
api -X POST "$SERVER/api/v1/rfid/cards/" -d '{"uid": "04A2B3C4"}'          # the card's number
api -X POST "$SERVER/api/v1/rfid/cards/<card id>/assign/" \
  -d '{"developer": <developer id>, "pin": "<PIN typed by the developer>", "pin_confirm": "<same PIN>"}'
```

(Assigning a card also sets the developer's purchase PIN: 4–6 digits, not 1111 or 1234.)
Don't know a card's number (UID)? Tap it on a door, then list the latest unknown cards:
`api "$SERVER/api/v1/rfid/events/?result=UNKNOWN_CARD&ordering=-event_time"` → `"uid"`.

### Step 9. Check it

Tap the developer's card on the door: it shows **"Welcome, Ada Kim"** (or "Goodbye, …"
on the way out). Then:

```bash
api "$SERVER/api/v1/attendance/occupancy/"                    # who is inside, per building
api "$SERVER/api/v1/rfid/events/?ordering=-event_time"        # the latest taps
```

### If a door doesn't work

| Log line (`sudo journalctl -u backend-tcp -f`) | Meaning / fix |
|---|---|
| nothing when tapping | The door doesn't reach the server: step 4, network, firewall (step 7) |
| `rejected ID='Door1' from <ip>` | Door not registered, wrong ID, or another IP than `allowed_ip`: steps 3 and 6 |
| `rejected ID='door 1' ...` | The door sends a different ID than the `code`: make them the same |
| door shows "Unknown card" / "Card not assigned" | Connection is fine; register / assign the card (step 8) |
| door shows "Card blocked" | The card was blocked; unblock it or give a new card |

Door screens show English; for Korean, install with `--device-language ko` (or set
`DEVICE_LANGUAGE=ko-kp` in `/etc/backend/backend.env` and restart the services), only if
the door screens can show Korean letters.

## 9. Connecting the till readers (payments)

A **till reader** is a card reader plugged into a **seller's PC**. A small **till program** on
that PC sends every card tap to the server and shows the server's answer. The developer
pays with their card **and their PIN**, typed on the seller's screen.

Unlike doors, a till reader is recognised by its **serial number (`SN`) and its `ID`**, not
by its IP, so it can move to another PC without any change here.

Do steps 1–2 once, on the server, in a terminal.

### Step 1. Sign in from the terminal

The same as step 1 of the door section (`SERVER`, `TOKEN` and the `api` helper).

### Step 2. Register each till reader

```bash
api -X POST "$SERVER/api/v1/rfid/devices/" \
  -d '{"code": "Reader1", "name": "Cafe till", "purpose": "TILL", "sn": "ZK2024A0001234"}'
```

- `code` must be exactly the **ID the till program sends** (`Reader1`, `Reader2`, …).
- `sn` is the reader's **serial number**, exactly as the program sends it (often printed on
  the reader). Each serial number can be registered once.
- Keep serial numbers confidential (labels covered): with the SN and ID someone could send
  fake taps. Nobody can be charged without the developer's PIN.

### Step 3. Set up the till program on the seller's PC

| Setting | Value |
|---|---|
Till programs talk to the server over **HTTPS** (port 443). The TCP port 9100 is only for
the doors.

| Setting | Value |
|---|---|
| Each tap | `POST https://<server-ip>/api/v1/rfid/events/`, `Content-Type: application/json`, body `{"SN": "ZK2024A0001234", "ID": "Reader1", "TYPE": "pay", "UID": "<card number>"}` |
| Heartbeat | every 30 s: `POST https://<server-ip>/api/v1/rfid/device/heartbeat/`, body `{"SN": "ZK2024A0001234", "ID": "Reader1"}` |
| Certificate | the program must trust the server's certificate (install the company's one on the server: "After installing", item 1; never switch verification off) |
| Timeout | about 5 seconds per request |

No API key is needed: `SN` + `ID` identify the reader. The heartbeat keeps the reader shown
as **connected** (heard from in the last 2 minutes), so the seller's till page finds it
automatically.

**Firewall:** the seller's PC needs port **443** to the server (opened by `prepare-server.sh`).

### Step 4. Check the reader is connected

```bash
api "$SERVER/api/v1/rfid/devices/?purpose=TILL"     # "online": true, "last_ip": the seller PC
```

### Step 5. Try a tap

Tap a developer's card **without** a purchase open. The reader shows **"No open purchase for
this reader"**: the connection works. (The seller has to start a purchase first.)

### Step 6. A payment, start to finish

1. The seller opens the **Till** page in the web app, picks the counter (the reader on this
   PC is chosen automatically), adds the goods and clicks **Scan card to buy**.
2. The developer taps their card. The till program gets the answer at once (below) and
   shows **"Ada Kim - enter PIN"**; the seller's screen shows who tapped.
3. The developer types their PIN on the seller's screen; the seller confirms. Done: the
   screen shows the total and the developer's new balance.

A tap is valid for **2 minutes**; a newer tap replaces it. The card number is never typed in.

### What the server answers to a tap

Every tap gets an answer right away, in the reply to the program's own request
(status `201`). For a registered card with a purchase open:

```json
{"id": 10504, "result": "ACCEPTED", "accepted": true, "direction": "",
 "display_message": "Ada Kim - enter PIN",
 "developer": {"id": 23125, "employee_number": "E001", "full_name": "Ada Kim", "department": ""},
 "purchase": 6543, "event_time": "2026-10-02T05:10:28.440673Z", "client_event_id": ""}
```

**All the answers** (the reader shows `display_message`):

| The card | `result` | `accepted` | Message on the reader | `purchase` |
|---|---|---|---|---|
| **Registered** to an active developer, purchase open | `ACCEPTED` | `true` | `Ada Kim - enter PIN` | the purchase id |
| Registered, **no purchase open** | `ACCEPTED` | `true` | `No open purchase for this reader` | `null` |
| Not registered | `UNKNOWN_CARD` | `false` | `Unknown card` | `null` |
| Registered, not given to anyone | `UNASSIGNED_CARD` | `false` | `Card not assigned` | `null` |
| Blocked (e.g. lost) | `BLOCKED_CARD` | `false` | `Card blocked` | `null` |
| Retired | `RETIRED_CARD` | `false` | `Card no longer valid` | `null` |
| Developer suspended or terminated | `INACTIVE_DEVELOPER` | `false` | `Not active - contact your manager` | `null` |

**Wrong reader `SN` or `ID`:** status `401` with
`{"error": {"code": "AUTHENTICATION_FAILED", "message": "Unknown till reader ID or serial number."}}`.
After **10 wrong attempts from one PC within 15 minutes**, that PC is blocked for 15 minutes.

**What the till program should do:**

- Show the message; **green light / beep** when `accepted` is `true`, **red** when `false`.
- `accepted: false` is still a **normal answer**: don't send the tap again.
- No answer (server unreachable, timeout of about 5 s): show **"Offline - cannot pay right
  now"** and **drop** the tap. Never store till taps to send later.
- The seller's screen also shows every tap live, accepted or not.

The messages are in English, or in Korean if the server was installed with
`--device-language ko` (only if the reader's screen can show Korean letters).

### If a till reader doesn't work

| What you see | Meaning / fix |
|---|---|
| "Unknown till reader ID or serial number." | `ID` or `SN` differs from step 2 (check spelling), or the reader is deactivated |
| No answer at all | The PC doesn't reach the server: address, port 443, firewall |
| "No open purchase for this reader" | The seller hasn't started a purchase, or chose another reader on the till page |
| The till page doesn't find the reader | No heartbeat: check step 3, and step 4 shows `"online": true` with the PC's IP |
| "Unknown card" / "Card not assigned" | Register the card and assign it to the developer (door section, step 8) |
| Certificate error in the till program | Install the company certificate on the server (and trust it on the PC) |

## 10. Upgrading to a newer version

Copy the new `backend-<new version>.tar.gz`, its `.sha256` and `install-backend.sh` into
the same folder, and run the same command:

```bash
sudo bash install-backend.sh
```

It takes the newest bundle in the folder, **makes a safety backup first**, then upgrades.
Data, accounts and settings are kept; the questions above are not asked again (pass an
option, e.g. `--language ko`, to change a setting).

## 11. Development on the offline server (optional)

To change the backend on this server (no internet), make a **development copy** for a
normal user account. It is separate from the installed backend: its own folder, its own
database and settings. Experiments never touch the real data.

### Set it up (once per developer)

```bash
sudo bash setup-dev.sh                    # for you, in ~/backend-dev
sudo bash setup-dev.sh --seed-demo        # ... with demo people, stores, attendance and sales
sudo bash setup-dev.sh --user kim         # for another user account
```

It installs git and the tools from the bundle, clones the code **with its full history**,
creates a Python environment with the development tools (pytest, ruff), a database
`backend_dev` on its own PostgreSQL instance (port 5433, not part of the backups) and a
settings file `~/backend-dev/.env` (DEBUG on). Add `--run-tests` to run
all tests at the end (10–15 minutes).

### Work in it (as your normal user, not root)

```bash
cd ~/backend-dev
.venv/bin/python manage.py createsuperuser           # a login for this copy
.venv/bin/python manage.py runserver 127.0.0.1:8000  # try it: http://127.0.0.1:8000/admin/
.venv/bin/pytest -q                                  # all tests
.venv/bin/ruff check . && .venv/bin/ruff format .    # lint and format
git status; git diff; git log --oneline              # see changes and history
git add -A && git commit -m "Describe the change"    # record a change
```

Set your name for commits once: `git config --global user.name "Kim"` and
`git config --global user.email kim@chonha.com`. Demo data commands (`seed_*`) work here
(not on the installed backend). To see the copy from another PC, run
`runserver 0.0.0.0:8000` and open port 8000 (`sudo ufw allow 8000/tcp`).

### Install your changes on this server

Commit first (the bundle contains only committed code), then build a bundle **without
internet**, from the packages the copy keeps in `.offline-cache/`, and install it:

```bash
cd ~/backend-dev
scripts/build_offline_bundle.sh --reuse .offline-cache 2026.10.07   # any new version name
sudo bash dist/install-backend.sh                                    # upgrades the installed backend
```

The installer makes a safety backup first, as with any upgrade. If your change needs a
Python package that isn't in the bundle, the build stops with a message: that change has to
be built on a machine with internet.

### When a newer bundle arrives

Run `sudo bash setup-dev.sh` again with the new bundle in the folder. It installs the new
packages and fetches the new code as the git branch `offline/main`, **without changing
your work**. Merge it when you're ready:

```bash
cd ~/backend-dev
git merge offline/main
.venv/bin/python manage.py migrate
```

## 12. Where things are

| What | Where |
|---|---|
| Application | `/opt/backend/current` (previous versions in `/opt/backend/releases/`) |
| Settings | `/etc/backend/backend.env` |
| Certificate | `/etc/backend/tls/cert.pem`, `key.pem` |
| Backups | `/var/backups/backend` (settings: `/etc/backend/backup.conf`) |
| Services | `backend-web` (web/API), `backend-ws` (live updates), `backend-tcp` (door devices, port 9100), `backend-worker` (background jobs) |

Restart everything after changing `/etc/backend/backend.env`:

```bash
sudo systemctl restart backend-web backend-ws backend-tcp backend-worker
```

## 13. Problems

| Symptom | What to do |
|---|---|
| `Checksum mismatch` | The copy is damaged or incomplete: copy the `.tar.gz` again |
| `This bundle is for Ubuntu 24.04 x86_64 only` | Install on Ubuntu Server 24.04, 64-bit Intel/AMD |
| A service shows `failed` | `sudo journalctl -u backend-web -n 100` (or the service named) |
| `/health/backup/` is not 200 | `sudo systemctl start backend-backup.service`, then `sudo journalctl -u backend-backup -n 50` |
| The site doesn't open from other PCs | Check the firewall (port 443) and that you use the server's IP or a name given with `--hosts` |
| Door scans don't arrive | `sudo journalctl -u backend-tcp -f`: register the door, or fix its IP |

More detail: `docs/OFFLINE_DEPLOYMENT.md` and `docs/BACKUP_AND_RESTORE.md` inside the
application (`/opt/backend/current/docs/`).
