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

If something fails, the message says what; see **14. Problems**.

## 5. Check it in a browser

From a PC in the company network open `https://<server-ip>/`. The browser warns about the
certificate the first time (the installer made a temporary one, see step 6): continue, and
sign in with the admin account.

## 6. After installing

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
4. **Doors:** see **9. Connecting the door devices**.
5. **Backups to a second place:** set `OFFSITE_DIR=` or `OFFSITE_RSYNC=` in
   `/etc/backend/backup.conf` (backups are made every night into `/var/backups/backend`).

## 7. Developing on the server

The development copies are yours (not root's) and **separate from the installed system**:
their own database (on its own PostgreSQL, port 5433) and settings. Experiments never
touch the real data. Run things **as your normal user**, without sudo.

### Backend (`~/backend-dev`, Python/Django)

```bash
cd ~/backend-dev
.venv/bin/python manage.py createsuperuser           # a login for the development copy
.venv/bin/python manage.py runserver 127.0.0.1:8000  # leave running (Ctrl+C stops)
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

To open the development frontend from another PC: `npm run dev -- -H 0.0.0.0`, open port
3000 (`sudo ufw allow 3000/tcp`) and add the server IP to `allowedDevOrigins` in
`next.config.ts`.

### Git (both copies)

Each copy is a git repository with the full history. Record your work:

```bash
git config --global user.name "Kim"; git config --global user.email kim@chonha.com   # once
git status; git diff; git log --oneline
git add -A && git commit -m "Describe the change"
```

## 8. Putting your changes live (without internet)

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

## 9. Connecting the door devices (attendance)

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

## 10. Updating with a newer kit

Copy the new kit folder to the server and run the same command:

```bash
sudo bash install-all.sh
```

The installed system is upgraded (with a safety backup first); settings, data and accounts
are kept. **Development copies are never overwritten:** the backend's new code arrives as the
git branch `offline/main` (merge it with `git merge offline/main`); for the frontend, compare
or merge from the new package by hand if needed.

## 11. Ports and services

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

## 12. Where things are

| What | Where |
|---|---|
| Installed backend / frontend | `/opt/backend/current`, `/opt/frontend/current` (older versions in `releases/`) |
| Node.js | `/opt/node/` (`node`, `npm`, `npx` in `/usr/local/bin`) |
| Backend settings | `/etc/backend/backend.env` (restart the backend services after editing) |
| Frontend settings | `/etc/systemd/system/frontend.service` (`API_URL`, `PORT`) |
| Certificate | `/etc/backend/tls/cert.pem`, `key.pem` |
| Backups | `/var/backups/backend` (settings: `/etc/backend/backup.conf`) |
| Development copies | `~/backend-dev` (settings `.env`), `~/frontend-dev` (settings `.env.local`) |

## 13. Without development copies

For a server that only runs the system: `sudo bash install-all.sh --dev-user none`.

## 14. Problems

| Symptom | What to do |
|---|---|
| `Checksum mismatch` | A file was damaged while copying: copy the kit again |
| `This kit is for Ubuntu 24.04 x86_64 only` | Install on Ubuntu Server 24.04, 64-bit Intel/AMD |
| `The frontend build failed` | The last lines show why; the full log path is printed. Often: not enough memory (4 GB), or the frontend loads fonts from the internet (`Failed to fetch … from Google Fonts`: the frontend must use `next/font/local`) |
| The page shows "502 Bad Gateway" | A service is down: `sudo systemctl status frontend backend-web` and the logs |
| The site doesn't open from other PCs | Firewall (port 443) and the server IP: `hostname -I` |
| `npm run dev` says the port is in use | Another dev server runs: stop it, or `npm run dev -- -p 3001` |
| Door scans don't arrive | See section 9, "If a door doesn't work" |
