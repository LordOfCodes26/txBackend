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
same for 8000).

**Live updates when opened directly (B).** The development frontend tells the browser to
connect live updates to the development backend at `127.0.0.1:8000`, which from another PC
is that PC itself. For B, add `WS_URL=ws://<server-ip>:8000` to `~/frontend-dev/.env.local`
and restart `npm run dev`. The tunnel (A) needs nothing.

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
**Cards → Assign card** with a card assign reader (section 11), or by command for a first test:

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

## 11. Connecting the till readers and card assign readers

These readers also use **TCP port 9100** (section 10). They are recognised by their **ID
alone** (`Reader1`, `Master1`, …), from any address, so a reader can move to another PC
without any change here. Each ID can be registered once.

- A **till reader** is plugged into a **seller's PC**. The developer pays with their card
  **and their PIN**, typed on the seller's screen.
- A **card assign reader** is used by staff to read new cards: registering a card and
  assigning it to a developer in the web app.

Sign in as in step 1 of section 10.

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

Server address and port `9100`, as for the doors (section 10, step 4). The reader sends:

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
| 9100 | RFID devices: doors, till and card assign readers (backend-tcp) | the devices |
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
| A till or card assign reader gets no answer or `CARD_NO` | See section 11, "If a reader doesn't work" |
