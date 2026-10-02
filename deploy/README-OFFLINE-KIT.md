# Offline kit: backend + frontend, running and development

Everything needed to run **and develop** the company management system on an Ubuntu
server **without internet**: the full source code of both projects with their git history,
every package they need, the runtimes, and one installer.

| File | What it is |
|---|---|
| `README.md` | This guide |
| `prepare-server.sh` | Prepares a freshly installed Ubuntu (name, clock, fixed IP, firewall) |
| `install-all.sh` | **Installs everything** (backend, frontend, development copies) |
| `backend-<version>.tar.gz` (+ `.sha256`) | Backend: source, git history, Python and Ubuntu packages |
| `management-app-offline.tar.gz` (+ `.sha256`) | Frontend: source, git history, all npm packages, Node.js |
| `install-backend.sh`, `setup-dev.sh` | Parts used by `install-all.sh` (can also run alone) |

What you get after installing:

| | Where | Address |
|---|---|---|
| The system (frontend + backend) | `/opt/frontend`, `/opt/backend` | `https://<server-ip>/` |
| Backend development copy | `~/backend-dev` | `http://127.0.0.1:8000` when running |
| Frontend development copy | `~/frontend-dev` | `http://127.0.0.1:3000` when running |

---

## 1. Before you start

- **Server:** Ubuntu Server **24.04** LTS, **x86_64** (Intel/AMD), freshly installed.
- **Disk:** at least **25 GB** free (the frontend's packages alone are ~1 GB per copy).
- **Memory:** at least **4 GB** (building the frontend needs it).
- **A normal user account** for development (e.g. `kim`), able to use `sudo`. Ubuntu's
  installer creates one.
- **Decide:**
  - the **server IP** in the company network, fixed (e.g. `192.168.1.10`);
  - the company **timezone** (e.g. `Asia/Pyongyang`);
  - the **language**: English `en` or Korean `ko` (web/API, and the door/till screens);
  - the **email of the first admin** (e.g. `admin@chonha.com`) and its password.

To see the server's IP: `hostname -I` (use the company-network one, not `127.0.0.1`).

## 2. Copy the kit

Copy the whole kit folder to the server (USB disk or internal network), e.g. to
`/home/kim/offline-kit/`, then:

```bash
cd /home/kim/offline-kit
ls      # README.md  install-all.sh  backend-*.tar.gz  management-app-offline.tar.gz  ...
```

## 3. Prepare the server (once, on a fresh Ubuntu)

```bash
sudo bash prepare-server.sh
```

It asks step by step (Enter keeps what is shown): server name, timezone and clock, the
**fixed IP** (card, IP with prefix like `192.168.1.10/24`, gateway, DNS or `none`), the
**firewall** (opens 22, 80, 443, 9100), and switches off Ubuntu's online auto-updates.
At the end it prints the next command.

- Over SSH, an IP change uses `netplan try`: press **Enter within 2 minutes** to keep it,
  then reconnect to the new IP. Otherwise it is undone by itself.
- `sudo bash prepare-server.sh --dry-run` shows what it would do without changing anything.
- Network and firewall already set up? `sudo bash prepare-server.sh --skip-network --skip-firewall`

## 4. Install everything

Run it **with sudo from your normal user** (so the development copies are made for you):

```bash
sudo bash install-all.sh
```

It asks for the server IP, timezone, languages and the first admin (email and password),
then works for 10–15 minutes:

1. **Backend**: database, services, backups, roles, door listener (port 9100);
2. **Node.js** into `/opt/node`;
3. **Frontend**: builds it on the server (no internet needed) and runs it as the service
   `frontend`; `https://<server-ip>/` now shows the frontend, while the backend keeps
   `/api/v1/`, `/admin/`, `/api/docs/`, `/ws/` (live updates) and `/health/`;
4. **Development copies** for you: `~/backend-dev` and `~/frontend-dev`;
5. **Checks** that everything answers.

Without questions:

```bash
sudo bash install-all.sh --server-ip 192.168.1.10 --timezone Asia/Pyongyang \
     --language ko --device-language en --admin-email admin@chonha.com
```

| Option | Meaning |
|---|---|
| `--server-ip IP` | The server's IP (`hostname -I`) |
| `--timezone Area/City` | Company timezone |
| `--language en\|ko` | Language of the web/API |
| `--device-language en\|ko` | Language on door/till screens (ko only if they show Korean letters) |
| `--admin-email EMAIL` | First admin account (asks for the password) |
| `--dev-user NAME` | Make the development copies for this user (default: you; `none` = no copies) |
| `--seed-demo` | Fill the backend development copy with demo data |
| `--yes` | Don't ask to confirm |

### Success looks like this

```
==> 5. Checks
    frontend service                   active
    https://localhost/ (frontend)      200
    https://localhost/health/ (backend) 200
    ...
Everything is installed.
  Open in a browser:   https://192.168.1.10/
```

If something fails, the message says what; see **16. Problems**.

## 5. Check it in a browser

From a PC in the company network open `https://<server-ip>/`. The browser warns about the
certificate the first time (the installer made a temporary one, see step 7): continue, and
sign in with the admin account.

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

Commands that use the installed backend run as its service user:

```bash
sudo -u backend bash -c 'set -a; . /etc/backend/backend.env; set +a; \
  cd /opt/backend/current && .venv/bin/python manage.py <command>'
```

1. **Certificate:** put the company's certificate in place, then reload nginx:
   `sudo cp cert.pem /etc/backend/tls/cert.pem; sudo cp key.pem /etc/backend/tls/key.pem; sudo systemctl reload nginx`
2. **Logins for each role** (`admin@chonha.com`, `boss@chonha.com`, …):
   `<command>` = `create_role_users --domain chonha.com`. Each password is **shown once**.
3. **Link the accounts:** a SELLER login to its store, BUILDING_OWNER / BUILDING_MANAGER
   logins to their buildings, DEVELOPER logins to developer profiles (in the web app).
4. **Doors and tills:** see **10. Connecting the door devices** and **11. Connecting the till
   readers**.
5. **Backups to a second place:** set `OFFSITE_DIR=` or `OFFSITE_RSYNC=` in
   `/etc/backend/backup.conf` (backups are made every night into `/var/backups/backend`).

## 8. Developing on the server

The development copies are yours (not root's) and **separate from the installed system**:
their own database (on its own PostgreSQL, port 5433) and settings. Experiments never
touch the real data. Run things **as your normal user**, without sudo.

### Backend (`~/backend-dev`, Python/Django)

```bash
cd ~/backend-dev
.venv/bin/python manage.py createsuperuser           # a login for the development copy
.venv/bin/uvicorn config.asgi:application --reload --port 8000   # leave running (Ctrl+C stops)
.venv/bin/pytest -q                                  # all tests (3–4 minutes)
.venv/bin/ruff check . && .venv/bin/ruff format .    # lint and format
.venv/bin/python manage.py seed_demo                 # demo data (development only)
```

### Frontend (`~/frontend-dev`, Next.js)

It is set to use the development backend above (`.env.local`: `API_URL=http://127.0.0.1:8000`).
Start the backend first, then in a second terminal:

```bash
cd ~/frontend-dev
npm run dev                  # http://127.0.0.1:3000, reloads on every change
npm run lint                 # ESLint
npx tsc --noEmit             # type check
npm run generate:api         # refresh the API types from the development backend
npm run build                # a production build, to check it builds
```

`uvicorn` serves the pages **and** the live updates (WebSockets), reloading on every code
change. (`manage.py runserver 127.0.0.1:8000` also works, but without live updates.)

### From another computer

The development servers listen only on the server itself (`127.0.0.1`), so nobody else on
the network reaches them by accident. To use them from your own PC's browser:

**A. SSH tunnel (recommended: nothing to change on the server).** Start the development
servers on the server as above, then on **your PC** open a terminal (Windows: PowerShell)
and keep this running:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 kim@<server-ip>
```

Now, on your PC: `http://localhost:3000` is the development frontend,
`http://localhost:8000/admin/` and `http://localhost:8000/api/docs/` the development
backend. It works exactly as on the server, live updates included, and is encrypted. It
needs SSH on the server: `setup-dev.sh` installs the SSH server from the kit; port 22 is
opened by `prepare-server.sh`.

**B. Directly over the network.** Start them on all addresses and open the ports:

```bash
cd ~/backend-dev  && .venv/bin/uvicorn config.asgi:application --reload --host 0.0.0.0 --port 8000
cd ~/frontend-dev && npm run dev -- -H 0.0.0.0
sudo ufw allow 3000/tcp; sudo ufw allow 8000/tcp
```

and add the server's IP to `allowedDevOrigins` in `~/frontend-dev/next.config.ts`
(otherwise the page loads but doesn't work). Then open `http://<server-ip>:3000` and
`http://<server-ip>:8000/admin/`. This is unencrypted and anyone on the network can reach
the development copies: close the ports again afterwards (`sudo ufw delete allow 3000/tcp`,
same for 8000). Live updates don't work this way yet (see the note below); use A for them.

**Note: live updates and the frontend.** The frontend tells the browser to open live
updates at its `API_URL`. That address is only right from the server itself (or through
the tunnel A). The installed system has the same problem (`ws://127.0.0.1:8001`): until the
frontend is fixed, pages that update live (occupancy, the till screen's card taps) only
refresh when reloaded or polled. The fix is in the frontend; see
`docs/FRONTEND_WEBSOCKET_URL.md` in the backend.

### Git (both copies)

Each copy is a git repository with the full history. Record your work:

```bash
git config --global user.name "Kim"; git config --global user.email kim@chonha.com   # once
git status; git diff; git log --oneline
git add -A && git commit -m "Describe the change"
```

## 9. Putting your changes live (without internet)

Commit first: only committed backend code goes into a bundle.

**Backend:**

```bash
cd ~/backend-dev
scripts/build_offline_bundle.sh --reuse .offline-cache 2026.10.20   # any new version name
sudo bash dist/install-backend.sh                                   # safety backup, then upgrade
```

If your change needs a Python package that isn't in the kit, the build stops with a
message: that change needs a machine with internet.

**Frontend:**

```bash
sudo bash ~/offline-kit/install-all.sh --skip-backend --dev-user none --frontend-from ~/frontend-dev
```

It builds the frontend from your copy (as it is now) and replaces the running one; the last
3 versions are kept in `/opt/frontend/releases/`. New npm packages can't be installed
without internet: add them on a machine with internet and bring a new kit.

## 10. Connecting the door devices (attendance)

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
step 4, the network cable, and the firewall (step 7 of this section). Give each door a **fixed IP** in the
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
| nothing when tapping | The door doesn't reach the server: step 4, network, firewall (step 7 of this section) |
| `rejected ID='Door1' from <ip>` | Door not registered, wrong ID, or another IP than `allowed_ip`: steps 3 and 6 |
| `rejected ID='door 1' ...` | The door sends a different ID than the `code`: make them the same |
| door shows "Unknown card" / "Card not assigned" | Connection is fine; register / assign the card (step 8) |
| door shows "Card blocked" | The card was blocked; unblock it or give a new card |

Door screens show English; for Korean, install with `--device-language ko` (or set
`DEVICE_LANGUAGE=ko-kp` in `/etc/backend/backend.env` and restart the services), only if
the door screens can show Korean letters.

## 11. Connecting the till readers (payments)

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

## 12. Keeping the offline server and the internet machine in step

The code lives in two places: the **offline server** (`~/backend-dev`, `~/frontend-dev`)
and the **internet machine** (where kits are built, and GitHub). Changes go both ways
with git, carried on a USB stick. In each development copy, the branch **`offline/main`**
is the code of the last kit installed; your work is on **`main`**.

```
internet machine                      USB stick                    offline server
  /root/backend, /root/frontend   ──── new kit folder ────►   install-all.sh → offline/main
                                  ◄─── *-changes.bundle ────   git bundle create
```

### A. Offline changes → internet machine

1. **On the offline server**, as your user, commit, then export the commits that the
   internet machine doesn't have yet (everything after the last kit):

   ```bash
   cd ~/backend-dev  && git status && git bundle create /media/usb/backend-changes.bundle  offline/main..main
   cd ~/frontend-dev && git status && git bundle create /media/usb/frontend-changes.bundle offline/main..main
   ```

   (`/media/usb` = where the USB stick is mounted; `lsblk` shows it. "Refusing to create
   an empty bundle" means there's nothing new in that copy.) The files are small: only
   your new commits.

2. **On the internet machine**, import, look, merge, test, push:

   ```bash
   cd /root/backend
   git bundle verify /media/usb/backend-changes.bundle        # "is okay"
   git fetch /media/usb/backend-changes.bundle main:offline-server
   git log --oneline main..offline-server                     # the offline commits
   git merge offline-server                                   # fix conflicts if any
   .venv/bin/pytest -q && git push && git branch -d offline-server
   ```

   The same for the frontend in `/root/frontend` with `frontend-changes.bundle`
   (check it with `npm run lint && npx tsc --noEmit && npm run build`).

`git bundle verify` needs the internet machine to have the commit the kit was built from
(it always does, unless that history was rewritten).

### B. Internet machine → offline server (a newer kit)

1. On the internet machine, commit and push everything (backend and frontend), then build
   the kit:

   ```bash
   cd /root/frontend && scripts/package-offline.sh
   cd /root/backend  && scripts/build_full_kit.sh 2026.10.20
   ```

   Commit the frontend **before** packaging: only committed changes reach `offline/main`.

2. Copy the new `dist/offline-kit-<version>/` folder to the offline server and run:

   ```bash
   sudo bash install-all.sh
   ```

   The installed system is upgraded (safety backup first; data, settings and accounts
   kept). **The development copies are never overwritten:** each gets the new code as
   `offline/main`; the installer says how many new commits there are.

3. Merge it into your work in each copy, when you're ready:

   ```bash
   cd ~/backend-dev  && git merge offline/main && .venv/bin/python manage.py migrate && .venv/bin/pytest -q
   cd ~/frontend-dev && git merge offline/main && npm run build
   ```

   **New npm packages** (when the merge changed `package.json`): the installer left the new
   kit's packages in `.offline-cache/node_modules`. Use them:

   ```bash
   cd ~/frontend-dev && rm -rf node_modules && cp -a .offline-cache/node_modules . && npm run build
   ```

### Rules that keep this simple

- **Commit before exporting or packaging.** Uncommitted changes don't travel.
- **One direction at a time:** bring offline changes in (A) **before** building the next
  kit (B); then the new kit already contains them and `git merge offline/main` is clean.
- **Don't rewrite history** (`git rebase`, `git push --force`) of commits that already
  travelled: the other side can't match them up anymore.

## 13. Ports and services

| Port | Who listens | Reachable from |
|---|---|---|
| 443 (80 redirects) | nginx: the frontend, and the backend's `/api/v1`, `/admin`, `/ws`, `/health` | the company network |
| 9100 | door devices (backend-tcp) | the doors |
| 3100 | the installed frontend (Next.js) | this server only |
| 8001 | the backend for the frontend server (nginx, no TLS) | this server only |
| 5432 / 5433 | PostgreSQL: installed system / development copies | this server only |
| 8000, 3000 | development servers, when you run them | this server only |

Services: `frontend`, `backend-web`, `backend-ws`, `backend-tcp`, `backend-worker`, `nginx`,
`postgresql`, `redis-server`. Logs: `sudo journalctl -u frontend -n 100` (or the service named).
Restart all: `sudo systemctl restart frontend backend-web backend-ws backend-tcp backend-worker`.

## 14. Where things are

| What | Where |
|---|---|
| Installed backend / frontend | `/opt/backend/current`, `/opt/frontend/current` (older versions in `releases/`) |
| Node.js | `/opt/node/` (`node`, `npm`, `npx` in `/usr/local/bin`) |
| Backend settings | `/etc/backend/backend.env` (restart the backend services after editing) |
| Frontend settings | `/etc/systemd/system/frontend.service` (`API_URL`, `PORT`) |
| Certificate | `/etc/backend/tls/cert.pem`, `key.pem` |
| Backups | `/var/backups/backend` (settings: `/etc/backend/backup.conf`) |
| Development copies | `~/backend-dev` (settings `.env`), `~/frontend-dev` (settings `.env.local`) |

## 15. Without development copies

For a server that only runs the system: `sudo bash install-all.sh --dev-user none`.

## 16. Problems

| Symptom | What to do |
|---|---|
| `Checksum mismatch` | A file was damaged while copying: copy the kit again |
| `This kit is for Ubuntu 24.04 x86_64 only` | Install on Ubuntu Server 24.04, 64-bit Intel/AMD |
| `The frontend build failed` | The last lines show why; the full log path is printed. Often: not enough memory (4 GB), or the frontend loads fonts from the internet (`Failed to fetch … from Google Fonts`: the frontend must use `next/font/local`) |
| The page shows "502 Bad Gateway" | A service is down: `sudo systemctl status frontend backend-web` and the logs |
| The site doesn't open from other PCs | Firewall (port 443) and the server IP: `hostname -I` |
| `npm run dev` says the port is in use | Another dev server runs: stop it, or `npm run dev -- -p 3001` |
| Door scans don't arrive | See section 10, "If a door doesn't work" |
| A till reader gets no answer or an error | See section 11, "If a till reader doesn't work" |
