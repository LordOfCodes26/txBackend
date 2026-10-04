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
  reach it on port **443** (HTTPS, also 80); the RFID devices (doors, till and card assign readers) send to TCP port **9100**.
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
  - the **username of the first admin** account.

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
Admin username [admin]:
Password: / Password (again):
```

Or give the answers up front:

```bash
sudo bash install-backend.sh --server-ip 192.168.1.10 --timezone Asia/Pyongyang \
     --language ko --device-language en --admin-user admin
```

| Option | Meaning |
|---|---|
| `--server-ip IP` | The server's IP in the company network (`hostname -I`). Used for the web address, the doors and the temporary certificate |
| `--timezone Area/City` | Company timezone |
| `--language en\|ko` | Web/API language when the browser doesn't choose one |
| `--device-language en\|ko` | Language on door and till reader screens |
| `--hosts "name1,name2"` | Extra host names / IPs clients use (the server's own are added) |
| `--admin-user NAME` | First admin account (asks for its password) |
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
| RFID devices (TCP) | `<server-ip>:9100` |
| The development copy's API, when running | `http://127.0.0.1:8000/api/v1/`, docs `http://127.0.0.1:8000/api/docs/` (no sign-in) |

### Open the documentation

1. Sign in at `https://<server-ip>/admin/` with an **admin** account (the first admin from
   the installation, or any ADMIN user marked as staff). The docs need you to be signed in.
2. Open `https://<server-ip>/api/docs/`: every endpoint, grouped (auth, developers, rfid,
   attendance, purchases, …), with what to send and what comes back.
3. **To try a request there:** open `POST /api/v1/auth/token/` → *Try it out* → put your
   username and password → *Execute* → copy the `access` value. Click **Authorize** at the top,
   paste it, *Authorize*. Now *Try it out* works on every endpoint. A token lasts 15 minutes.

### Use it from a program or the terminal

Every request (except signing in) sends the token: `Authorization: Bearer <access token>`.

```bash
curl -sk https://<server-ip>/api/v1/auth/token/ -H 'Content-Type: application/json' \
  -d '{"username": "admin", "password": "..."}'            # → {"access": "...", "refresh": "..."}
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

2. **Logins for each role** (usernames `admin`, `boss`, …):

   ```bash
   sudo -u backend bash -c 'set -a; . /etc/backend/backend.env; set +a; \
     cd /opt/backend/current && .venv/bin/python manage.py create_role_users'
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

All devices (doors, till readers, card assign readers) talk to the server over **TCP port
9100** (service `backend-tcp`, running right after the install). Each tap is one packet
between `$` signs, and the server answers every packet with one line:

| Device | Packet | Answers |
|---|---|---|
| Door, way in | `$ID:Door1,TYPE:Input,UID=DC62B3E3$` | `CARD_OK`, `CARD_NO`, `CARD_DENIED` |
| Door, way out | `$ID:Door1,TYPE:Output,UID=DC62B3E3$` | `CARD_OK`, `CARD_NO`, `CARD_DENIED` |
| Till reader | `$ID:Reader1,TYPE:Pay,UID=DC62B3E3$` | `CARD_OK`, `CARD_NO` |
| Card assign reader | `$ID:Master1,TYPE:Master,UID=DC62B3E3$` | `CARD_OK`, `CARD_NO` |

Each answer ends with a line break (`\r\n`). The server **rejects every device until it is
registered** (answer `CARD_NO`).

A door is accepted only when its **ID** is registered **and** the packet comes from that
unit's **fixed IP address**. A door usually has **several units** (readers) that all send the
same ID, for example Door1:

| Unit | IP | Sends |
|---|---|---|
| Door1-1 | 192.168.100.151 | `ID:Door1,TYPE:Input` |
| Door1-2 | 192.168.100.152 | `ID:Door1,TYPE:Input` |
| Door1-3 | 192.168.100.153 | `ID:Door1,TYPE:Output` |
| Door1-4 | 192.168.100.154 | `ID:Door1,TYPE:Output` |

Each unit is registered as **its own device**: the same `code` (`Door1`), its own `name`
(`Door1-1`) and its own `allowed_ip`. The direction (in / out) comes from each packet's
`TYPE`.

Do these steps once, on the server (a terminal on the server itself).

### Step 1. Sign in from the terminal

Use the admin account from the installation. The sign-in lasts 15 minutes: if a later
command answers `token_not_valid`, run this step again.

```bash
SERVER=https://localhost
read -p "Admin username: " LOGIN; read -s -p "Password: " PASSWORD; echo
TOKEN=$(LOGIN="$LOGIN" PASSWORD="$PASSWORD" python3 -c \
  'import json, os; print(json.dumps({"username": os.environ["LOGIN"], "password": os.environ["PASSWORD"]}))' \
  | curl -sk "$SERVER/api/v1/auth/token/" -H 'Content-Type: application/json' -d @- \
  | python3 -c 'import sys, json; print(json.load(sys.stdin).get("access", ""))')
[ -n "$TOKEN" ] && echo "Signed in." || echo "Sign-in failed: check the username and password."
api() { curl -sk -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' "$@"; echo; }
api "$SERVER/api/v1/auth/me/"          # shows your account: the sign-in worked
```

### Step 2. Create the buildings

```bash
api -X POST "$SERVER/api/v1/rfid/buildings/" -d '{"code": "B1", "name": "Building 1"}'
api -X POST "$SERVER/api/v1/rfid/buildings/" -d '{"code": "B2", "name": "Building 2"}'
api "$SERVER/api/v1/rfid/buildings/"   # note each building's "id"
```

(Buildings can also be created in the web app: Readers → Buildings.)

### Step 3. Register every door unit

One device per **unit**, with the door's ID as `code`, the unit's name, its building (ids
from step 2) and its fixed IP:

```bash
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Door1", "name": "Door1-1", "building": 1, "allowed_ip": "192.168.100.151"}'
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Door1", "name": "Door1-2", "building": 1, "allowed_ip": "192.168.100.152"}'
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Door1", "name": "Door1-3", "building": 1, "allowed_ip": "192.168.100.153"}'
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Door1", "name": "Door1-4", "building": 1, "allowed_ip": "192.168.100.154"}'
# ...and Door2-1 ... Door2-6 (192.168.100.201 ... .206) with "code": "Door2", "building": 2
api "$SERVER/api/v1/rfid/devices/?purpose=ATTENDANCE"
```

(Or in the web app: Readers → New device, kind **Attendance door**.)

- `code` must be exactly the **ID the door sends** (`Door1`, `Door2`).
- Units sharing an ID each need **their own IP**; the same IP can't be used twice for one ID.
- The answer also shows an `api_key`: TCP doors don't need it, ignore it.

### Step 4. Point each unit at the server

In each door unit's own settings:

| Setting | Value |
|---|---|
| Server address | the server IP chosen during the install, e.g. `192.168.100.10` (shown at the end of the install; `grep SERVER_IP /etc/backend/backend.env`) |
| Port | `9100` |
| ID | `Door1` (or `Door2`), the same as the `code` in step 3 |
| Own IP | fixed, the same as `allowed_ip` in step 3 (set it in the unit or reserve it in the router) |

### Step 5. Check each unit reaches the server

Watch the device log and tap any card on a unit:

```bash
sudo journalctl -u backend-tcp -f
```

The unit answers `CARD_NO` for a card that isn't registered yet: the connection works. If
the unit isn't registered with this IP, a line like this appears (Ctrl+C stops watching):

```
RFID TCP: rejected ID='Door1' from 192.168.100.155 (no such device/IP)
```

`192.168.100.155` is the address the unit really uses: fix the unit's IP, or register that
address (step 3). Nothing appears at all? The unit doesn't reach the server: check step 4,
the network cable and the firewall (step 6 of this section).

### Step 6. Firewall (if the server has one)

Allow port 9100 from the device network only:

```bash
sudo ufw allow from 192.168.100.0/24 to any port 9100 proto tcp
```

### Step 7. Register developers and their cards

A door opens only for a card assigned to an active developer. Usually in the web app:
**Cards → Assign card** with a card assign reader (section 9), or by command for a first test:

```bash
api -X POST "$SERVER/api/v1/developers/" -d '{"employee_number": "E001", "full_name": "Ada Kim"}'
api -X POST "$SERVER/api/v1/rfid/cards/" -d '{"uid": "DC62B3E3"}'          # the card's number
api -X POST "$SERVER/api/v1/rfid/cards/<card id>/assign/" \
  -d '{"developer": <developer id>, "building": 1, "pin": "<PIN typed by the developer>", "pin_confirm": "<same PIN>"}'
```

(Assigning a card also sets the developer's home building and purchase PIN: 4–6 digits, not
1111 or 1234.) Don't know a card's number (UID)? Tap it on a door, then list the latest
unknown cards: `api "$SERVER/api/v1/rfid/events/?result=UNKNOWN_CARD&ordering=-event_time"` → `"uid"`.

### Step 8. Check it

Tap the developer's card on a unit: it answers `CARD_OK` (the door opens). Then:

```bash
api "$SERVER/api/v1/attendance/occupancy/"                    # who is inside, per building
api "$SERVER/api/v1/rfid/events/?ordering=-event_time"        # the latest taps
```

### What a door answers

| The card | Answer |
|---|---|
| Assigned to an active developer (also a repeated tap) | `CARD_OK` |
| Not registered | `CARD_NO` |
| Registered but not given to anyone, blocked, retired, or the developer is suspended / terminated | `CARD_DENIED` |
| The packet can't be used (unknown ID or IP, wrong `TYPE`, bad UID) | `CARD_NO` and a log line |

### If a door doesn't work

| What you see | Meaning / fix |
|---|---|
| nothing in the log when tapping | The unit doesn't reach the server: step 4, network, firewall (step 6) |
| `rejected ID='Door1' from <ip>` | That ID isn't registered with this IP: steps 3 and 4 |
| `rejected ID='door 1' ...` | The unit sends a different ID than the `code`: make them the same |
| `invalid frame from <ip>` | Wrong `TYPE` for the device (doors send `Input` / `Output`) or a bad UID |
| `CARD_NO` for a developer's card | The card isn't registered: step 7 |
| `CARD_DENIED` | The card isn't assigned, is blocked or retired, or the developer isn't active |

## 9. Connecting the till readers and card assign readers

These readers also use **TCP port 9100** (section 8). They are recognised by their **ID
alone** (`Reader1`, `Master1`, …), from any address, so a reader can move to another PC
without any change here. Each ID can be registered once.

- A **till reader** is plugged into a **seller's PC**. The developer pays with their card
  **and their PIN**, typed on the seller's screen.
- A **card assign reader** is used by staff to read new cards: registering a card and
  assigning it to a developer in the web app.

Sign in as in step 1 of section 8.

### Step 1. Register the readers

```bash
api "$SERVER/api/v1/sellers/"           # note each seller's "id"
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Reader1", "name": "Cafe till", "purpose": "TILL", "seller": 1}'
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Reader2", "name": "Shop till", "purpose": "TILL", "seller": 2}'
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Master1", "name": "Front desk", "purpose": "ENROLL"}'
api -X POST "$SERVER/api/v1/rfid/devices/" -d '{"code": "Master2", "name": "Office", "purpose": "ENROLL"}'
```

(Or in the web app: Readers → New device, kind **Till reader** with its **Seller**, or **Card
assign reader**.) `code` must be exactly the **ID the reader sends**. A till reader belongs
to one seller (a seller can have several); only that seller's purchases use it.

### Step 2. Point each reader at the server

Server address and port `9100`, as for the doors (section 8, step 4). The reader sends:

| Reader | Packet |
|---|---|
| Till reader | `$ID:Reader1,TYPE:Pay,UID=DC62B3E3$` (`ID:ID:Reader1` works too) |
| Card assign reader | `$ID:Master1,TYPE:Master,UID=DC62B3E3$` |

### Step 3. A payment, start to finish

1. The seller opens the **Till** page in the web app, picks the counter (the seller's till
   reader is used; a seller with several picks one), adds the goods and clicks **Scan card
   to buy**.
2. The developer taps their card within 2 minutes. The reader gets `CARD_OK`; the seller's
   screen shows who tapped.
3. The developer types their PIN on the seller's screen; the seller confirms. Done: the
   screen shows the total and the developer's new balance.

Only a purchase waiting for a card (**Scan card to buy** pressed, not yet tapped, at most 2
minutes ago) takes a tap; old unpaid purchases never do. A tap is valid for **2 minutes**; to
take another card, click **Scan card to buy** again. The card number is never typed in.

### Step 4. Registering and assigning cards with a card assign reader

In the web app, **Cards → New** (register only) or **Cards → Assign card**: pick the reader,
tap the card. Its number appears on the page at once; a new card is registered by the tap.
Then choose the developer, their building, and the developer types their PIN twice.

### What the readers answer

| Reader | The card | Answer |
|---|---|---|
| Till | Assigned to an active developer, and a purchase is waiting for a card on this reader | `CARD_OK` |
| Till | Anything else (no purchase waiting, not registered, not assigned, blocked, retired, developer not active) | `CARD_NO` |
| Card assign | Already registered | `CARD_OK` |
| Card assign | New: registered by this tap | `CARD_NO` |
| Any | The packet can't be used (unknown ID, wrong `TYPE`, bad UID) | `CARD_NO` and a log line |

### If a reader doesn't work

| What you see | Meaning / fix |
|---|---|
| nothing in `sudo journalctl -u backend-tcp -f` when tapping | The reader doesn't reach the server: address, port 9100, firewall |
| `rejected ID='Reader1' from <ip>` | The ID isn't registered (or the reader is deactivated): step 1, check the spelling |
| `invalid frame from <ip>` | Wrong `TYPE` (tills send `Pay`, card assign readers `Master`) or a bad UID |
| Till: `CARD_NO` for a developer's card | No purchase is waiting: click **Scan card to buy** first (and check the purchase uses this reader); or register the card and assign it |

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
.venv/bin/uvicorn config.asgi:application --reload --port 8000   # try it: http://127.0.0.1:8000/admin/
.venv/bin/pytest -q                                  # all tests
.venv/bin/ruff check . && .venv/bin/ruff format .    # lint and format
git status; git diff; git log --oneline              # see changes and history
git add -A && git commit -m "Describe the change"    # record a change
```

Set your name for commits once: `git config --global user.name "Kim"` and
`git config --global user.email kim@chonha.com`. Demo data commands (`seed_*`) work here
(not on the installed backend). `uvicorn` serves the pages and the live updates and reloads
on every change.

To use the copy from **another PC's browser**, the simplest and safest way is an SSH tunnel:
on your PC run `ssh -L 8000:127.0.0.1:8000 kim@<server-ip>` and keep it open, then browse
`http://localhost:8000/admin/` or `http://localhost:8000/api/docs/` on your PC.
(`setup-dev.sh` installs the SSH server.) Or run uvicorn with `--host 0.0.0.0`, open the
port (`sudo ufw allow 8000/tcp`) and browse `http://<server-ip>:8000/`: unencrypted and
open to the whole network, so close the port again afterwards.

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
