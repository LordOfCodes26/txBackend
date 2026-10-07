# Developing the backend

How to work on the backend on the offline Ubuntu server: the development copy, its ports
and settings, how to change them, how to test with the door and reader devices, demo data,
tests, and how to put a change live.

Installing the live system: [../deploy/README-OFFLINE-KIT.md](../deploy/README-OFFLINE-KIT.md).
Code conventions: [../README.md](../README.md#conventions).

## Contents

1. [Live system and development copy](#1-live-system-and-development-copy)
2. [Ports](#2-ports)
3. [Set up a development copy](#3-set-up-a-development-copy)
4. [Start it](#4-start-it)
5. [Change a port](#5-change-a-port)
6. [Settings (.env)](#6-settings-env)
7. [Test with door and reader devices](#7-test-with-door-and-reader-devices)
8. [Demo data: add, remove](#8-demo-data-add-remove)
9. [Make a change](#9-make-a-change)
10. [Put your change live](#10-put-your-change-live)
11. [Problems](#11-problems)

## 1. Live system and development copy

The server runs two separate backends:

| | Live system | Development copy |
|---|---|---|
| What | what the company uses: doors, tills, money | a developer's working copy, for changes and experiments |
| Code | `/opt/backend/current` | `~/backend-dev` (a git repository) |
| Settings | `/etc/backend/backend.env` | `~/backend-dev/.env` (`DEBUG` on) |
| Database | `backend` on PostgreSQL port 5432, backed up | `backend_dev` on its **own** PostgreSQL (cluster `dev`), port 5433, not backed up |
| Runs | as services (`backend-web`, `backend-ws`, `backend-tcp`, `backend-worker`), always | only while you start it (section 4) |
| Web app | `/opt/frontend/current` (service `frontend`) | `~/frontend-dev` |

Nothing done in the development copy reaches the live data. Its database, demo data and
test runs stay separate. The only things they share are the server and its ports, so the
development copy uses **different ports** (next section).

## 2. Ports

| What | Live system | Development copy |
|---|---|---|
| Web app and API, for browsers | 443 and 80 (nginx) | — |
| Backend API | unix socket (gunicorn, `backend-web`) | **8000** (uvicorn) |
| Live updates (WebSockets) | 127.0.0.1:8091 (`backend-ws`) | 8000, the same port as the API |
| Internal API address for the web app | 127.0.0.1:8001 (nginx) | — |
| Web app (Next.js) | 127.0.0.1:3100 (`frontend`) | 3000 (`npm run dev`) |
| Door and reader devices (TCP) | **9100** (`backend-tcp`) | **9101** |
| PostgreSQL | 5432 | 5433 |
| Redis | 6379, database 0 | the same server, database 1 |

**The development door listener uses 9101.** Two programs can't listen on one port, and
`backend-tcp` holds 9100. Copies set up before 2026-10-04 have no `RFID_TCP_PORT` line in
`~/backend-dev/.env`: add `RFID_TCP_PORT=9101` yourself.

`sudo mgmt change-door-port` and `change-web-port` refuse the development ports (8000,
3000, 9101) and the live system's own ports, so the two never collide.

## 3. Set up a development copy

The installer makes it, once per developer. It needs no internet. In the kit folder:

```bash
sudo bash install-all.sh --dev-user kim               # with the installation
sudo bash install-all.sh --dev-user kim --seed-demo   # ... with demo data (section 8)
sudo bash setup-dev.sh --user kim                     # only the backend copy
sudo bash setup-dev.sh --user kim --run-tests         # ... and run all tests at the end
```

`--dev-user` defaults to the user running `sudo`. `--dev-user none` makes no copies.

What you get:

- `~/backend-dev`: the source with its full git history, a Python environment with the
  development tools (pytest, ruff), the database `backend_dev` and a `.env` (DEBUG on).
- `~/frontend-dev`: the web app's source and `node_modules`. Its `.env.local` points at the
  development backend (`API_URL=http://127.0.0.1:8000`).

Running the installer again is safe. It upgrades the live system and never overwrites your
work. A newer kit's code arrives as the git branch `offline/main` (section 10).

Set your name for commits once:

```bash
git config --global user.name "Kim"
git config --global user.email kim@example.com
```

## 4. Start it

Run these **as your normal user**, without sudo. Use one terminal for each.

**Backend** (API, admin, API docs and WebSockets, reloads on every change):

```bash
cd ~/backend-dev
.venv/bin/python manage.py createsuperuser
.venv/bin/uvicorn config.asgi:application --reload --port 8000
```

You need `createsuperuser` only once, to make a login for this copy. Then open
`http://127.0.0.1:8000/admin/`. The API docs are at `/api/docs/`. `manage.py runserver`
also works, but it has no live updates.

**Door listener** (only when you test with devices, section 7):

```bash
cd ~/backend-dev
.venv/bin/python manage.py run_rfid_tcp
```

It uses the port from `RFID_TCP_PORT` in `.env`, which is 9101. For a single run on another
port, add `--port 9102`. It doesn't reload: stop it with Ctrl+C and start it again after a
code change.

**Frontend:**

```bash
cd ~/frontend-dev
npm run dev
```

Then open `http://127.0.0.1:3000`.

**Background worker:** not needed. The backend has no background jobs yet.

**From another PC:** use an SSH tunnel. Nothing changes on the server, and it is encrypted.
On your PC, keep this running:

```bash
ssh -L 3000:127.0.0.1:3000 -L 8000:127.0.0.1:8000 kim@<server-ip>
```

Then browse `http://localhost:3000` on your PC.

The other way is to start the servers on all addresses: `uvicorn ... --host 0.0.0.0` and
`npm run dev -- -H 0.0.0.0`. This also needs:

- the ports open in ufw;
- the server's IP in `allowedDevOrigins` in `~/frontend-dev/next.config.ts`;
- `WS_URL=ws://<server-ip>:8000` in `~/frontend-dev/.env.local`.

That is unencrypted and open to the whole network, so close the ports again afterwards. The
details are in the kit README, section 8.

## 5. Change a port

### Development API port

1. Start uvicorn with the new port: `--port 8200`.
2. Point the development frontend at it, in `~/frontend-dev/.env.local`:
   ```
   API_URL=http://127.0.0.1:8200
   ```
   If you set `WS_URL` there, change its port too.
3. Restart `npm run dev`. It reads `.env.local` only when it starts.

Don't use a port the live system uses (section 2). If you take a port other than 8000, also
use it in the SSH tunnel.

### Development door port

Set it in `~/backend-dev/.env`, then restart `run_rfid_tcp`:

```
RFID_TCP_PORT=9101
```

For a single run, use `run_rfid_tcp --port N` instead. Then set the same port in the device
simulator or the test device (section 7).

### Development database port

The installer makes the development PostgreSQL cluster `dev` on port 5433 (`PG_PORT` in
`deploy/setup-dev.sh`). The copy finds its database through `DATABASE_URL` in `.env`:

```
DATABASE_URL=postgres://backend_dev:<password>@localhost:5433/backend_dev
```

To move it:

1. Change `port` in `/etc/postgresql/<version>/dev/postgresql.conf`.
2. Run `sudo pg_ctlcluster <version> dev restart`.
3. Change `DATABASE_URL`.

A later `setup-dev.sh` expects 5433 again, so only do this if 5433 is really taken.

### Live door port

```bash
sudo mgmt change-door-port 9200
```

It does the following:

- sets `RFID_TCP_PORT` in `/etc/backend/backend.env`;
- restarts `backend-tcp` and checks that it answers. If it doesn't come up on the new port,
  the old one is put back;
- moves the ufw rule, keeping who may use the port.

Upgrades keep the setting. Then **set every door and reader device to the new port**: until
then they aren't heard.

### Live web ports

```bash
sudo mgmt change-web-port 8080 8443        # HTTP first, then HTTPS
```

It changes nginx's ports and checks that the site answers. If it doesn't, the old ports are
put back. It also moves the firewall rules, and upgrades keep the setting. The address
becomes `https://<server-ip>:8443/`, so tell the users.

The internal ports (8001, 8091, 3100) are fixed by the installer. Only the server itself
can reach them, so there is no need to change them.

## 6. Settings (.env)

`~/backend-dev/.env` is made from [`.env.example`](../.env.example). It is read at start, so
restart uvicorn or `run_rfid_tcp` after a change. These are the settings you are most likely
to change:

| Setting | Development copy | What it does |
|---|---|---|
| `DATABASE_URL` | `backend_dev` on port 5433 | the database |
| `REDIS_URL` | `redis://localhost:6379/1` | cache and live updates (database 1; the live system uses 0) |
| `RFID_TCP_PORT` | `9101` | the door listener's port |
| `RFID_TCP_HOST` | `0.0.0.0` | `127.0.0.1` = only from this server |
| `TEST_CONSOLE_ENABLED` | off | `true` turns on `/test-console/` (door scans and till without devices) |
| `TIME_ZONE`, `LANGUAGE_CODE`, `DEVICE_LANGUAGE` | copied from the live system | |
| `ATTENDANCE_DIRECTION_RULE` | `device` | in/out from the door unit; run `manage.py rebuild_attendance` after a change |
| `RFID_DEBOUNCE_SECONDS` | 10 | a second tap of the same card within this time is a duplicate |
| `PURCHASE_PIN_MAX_ATTEMPTS`, `PURCHASE_PIN_LOCKOUT_MINUTES` | 5, 15 | PIN lockout |
| `FINANCE_MAX_DEPOSIT` | 1000.00 | largest single deposit (Excel opening balances too) |
| `DATA_RESET_BACKUP_DIR` | `reset-backups` next to `MEDIA_ROOT` | where the data reset puts its backup |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

The full list, with defaults, is in [`config/settings/base.py`](../config/settings/base.py).
Tests don't use your `.env`, except for finding the database: `config/settings/test.py`
fixes the rest.

## 7. Test with door and reader devices

The devices speak raw TCP to the door listener (protocol:
[DEVICE_INTEGRATION.md](DEVICE_INTEGRATION.md)). Test the development copy with the
**device simulator** (`tools/device-simulator`) or with a spare real device:

1. Start the development door listener (section 4). It listens on 9101.
2. Point the simulator at the server and **port 9101**:
   ```
   python3 device_simulator.py --tap Reader1 DC62B3E3 --server <server-ip> --port 9101
   ```
   Make sure it isn't set to 9100: that is the live system.
3. The **development** database must know the device. Register it under **Readers → New
   device** in the development web app, or use the demo data:
   - **Till readers** (`Reader1`) and **card assign readers** (`Master1`) are recognised by
     their ID alone, from any address.
   - **Door units** are recognised by their ID **and the address they send from**.
     `Door1-1` must come from 192.168.100.151.
     - Simulator on the server with server `127.0.0.1`: register a door unit with IP
       `127.0.0.1`.
     - Simulator on another PC: use that PC's IP, or give that PC the door addresses (see
       the simulator's README).
4. If the simulator runs on another PC, open 9101 for that PC only, and close it again when
   you are done:
   ```bash
   sudo ufw allow from <pc-ip> to any port 9101 proto tcp
   sudo ufw delete allow from <pc-ip> to any port 9101 proto tcp
   ```

Every packet and every answer appears in the development web app's **TCP log** page
(admins) and in the listener's terminal. A device that gets `CARD_NO` is explained there.

**Without devices:** set `TEST_CONSOLE_ENABLED=true` in `.env`, restart uvicorn and open
`http://127.0.0.1:8000/test-console/`. It simulates door scans and the till through the
normal pipeline. It skips only the TCP connection. Never set this in
`/etc/backend/backend.env`.

## 8. Demo data: add, remove

Demo data exists only in the development copy. The `seed_*` commands refuse to run when
`DEBUG` is off, which is always the case on the live system.

**Add** it with `--seed-demo` (section 3), or at any time. Run the commands in this order:

```bash
cd ~/backend-dev
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py seed_more
.venv/bin/python manage.py seed_attendance_month
.venv/bin/python manage.py seed_purchases_month
```

| Command | What it adds |
|---|---|
| `seed_demo` | users for each role, buildings, door units, readers, sellers, goods |
| `seed_more` | 200 more developers, PINs, Demo Bakery, today's scans |
| `seed_attendance_month` | a month of door history up to yesterday |
| `seed_purchases_month` | last month's purchases (`--month 2026-09` for another month) |

Each part is made once, so running a command again adds only what is missing. The demo login
password and PIN are printed **once**: keep them.

Demo layout:

- Buildings B1 and B2.
- Door units `Door1-1`..`4` (192.168.100.151–154) and `Door2-1`..`6` (.201–.206).
- Till readers `Reader1`–`4`, each tied to its seller.
- Card assign readers `Master1` and `Master2`.

**Remove** it with the data reset. It deletes developers, cards and all activity, and keeps
users, roles, readers, buildings, sellers and goods. It makes a backup first:

```bash
cd ~/backend-dev
.venv/bin/python manage.py reset_data
```

`reset_data` asks you to type `DELETE ALL DATA`. The same reset is in the web app under
**System → Data reset** (Admin), and it works on the live system too, always with a backup
first.

To start with a completely empty development database instead, stop uvicorn and
`run_rfid_tcp`, then:

```bash
sudo -u postgres dropdb -p 5433 backend_dev
sudo -u postgres createdb -p 5433 -O backend_dev backend_dev
cd ~/backend-dev
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
```

## 9. Make a change

```bash
cd ~/backend-dev
git switch -c my-change
.venv/bin/pytest -q
.venv/bin/pytest -q tests/test_tcp_log.py
.venv/bin/ruff check . && .venv/bin/ruff format .
git add -A && git commit -m "Describe the change"
```

- `git switch -c` makes a branch for the change. It is optional.
- `pytest -q` runs all tests, which takes a few minutes. Give it a file to run only that
  file.
- The tests make their own throw-away database (`test_backend_dev`) and never touch
  `backend_dev`.

**Model changes:**

```bash
.venv/bin/python manage.py makemigrations <app>
.venv/bin/python manage.py migrate
```

`makemigrations` writes `apps/<app>/migrations/00xx_...`, and `migrate` applies it to the
development database. Read the new migration before committing. If `makemigrations` asks a
question (a default for a new field), answer it, or give the field a default in the model.
Test migrations here first: switching back to an older release doesn't undo them on the live
system.

**Where things go** (details in [../README.md](../README.md#conventions)):

- Business rules go in `apps/<app>/services.py`, not in views or serializers. Errors are
  `DomainError` subclasses with a stable `code`.
- New permissions go in `apps/accounts/rbac.py`. `migrate` adds them, and only **new**
  permissions are granted to roles. `manage.py sync_rbac` runs the same step by itself.
- Ledgers and logs are append-only (database triggers). Correct them with a new entry, never
  by editing.
- User-facing texts use `_("...")`, with a Korean translation in `locale/ko_KP`
  ([../README.md](../README.md#translations-english--korean-dprk-usage)).
  `tests/test_i18n.py` fails when one is missing.
- The door protocol lives in `apps/rfid/tcp.py`, and device lookup by ID and IP in
  `apps/rfid/authentication.py`.
- Excel import and export live in `apps/spreadsheets`. Imports reuse the forms' serializers
  and services, so they follow the same rules.

**Checks** for when something looks wrong in the data:

| Command | What it does |
|---|---|
| `.venv/bin/python manage.py check_ledger` | checks developer balances against their ledger |
| `.venv/bin/python manage.py check_inventory` | checks stock against the movements |
| `.venv/bin/python manage.py rebuild_attendance` | rebuilds attendance from the door scans |

## 10. Put your change live

Only **committed** changes go live, so commit first. Then:

```bash
sudo mgmt deploy-backend    # ~/backend-dev: safety backup, new release, migrations, restart
sudo mgmt deploy-frontend   # ~/frontend-dev: build (a few minutes), restart
sudo mgmt deploy-all        # both
```

Each deploy is a new release next to the running one. If it doesn't start, the previous one
is put back and the command says why. Uncommitted changes are listed and not deployed. To
deploy another developer's copy, add `--user kim`. `sudo mgmt status` shows the services,
ports and health checks.

A change that needs a **new Python package** must find it in
`~/backend-dev/.offline-cache/wheelhouse`, and a new npm package must be in
`~/frontend-dev/node_modules`. Otherwise it has to come with a kit built on the internet
machine.

**Send your commits to the internet machine** (USB stick), or the next kit won't have them:

```bash
cd ~/backend-dev
git bundle create /media/usb/backend-changes.bundle offline/main..main
```

On the internet machine, run `git fetch /media/usb/backend-changes.bundle main:offline-server`,
then review and merge.

**When a newer kit arrives**, run its `install-all.sh`. Your copy gets the new code as the
branch `offline/main`. Merge it when you're ready:

```bash
cd ~/backend-dev
git merge offline/main
.venv/bin/python manage.py migrate
```

## 11. Problems

| Symptom | Cause and fix |
|---|---|
| `address already in use` starting uvicorn or `run_rfid_tcp` | Another program has the port, often the live system (section 2). Use 8000 and 9101. To find the program: `sudo ss -ltnp \| grep :9101`. |
| The development web app shows live data | `~/frontend-dev/.env.local` points at the live API. Set `API_URL=http://127.0.0.1:8000` and restart `npm run dev`. |
| The simulator gets `CARD_NO` | Read the reason in the TCP log or the listener's terminal. Either the device isn't registered in the **development** database, a door unit sends from another IP, or you sent to 9100 (live) instead of 9101. |
| The simulator gets no answer at all | The development listener isn't running, or ufw blocks 9101 (simulator on another PC). |
| `seed_demo only runs with DEBUG=True` | You are in the live system's folder. Run it in `~/backend-dev`. |
| `Demo data already exists.` | Already seeded. To start again, use the data reset (section 8). |
| `could not connect to server ... port 5433` | The development PostgreSQL isn't running: `sudo pg_ctlcluster <version> dev start`. |
| `mgmt deploy-backend`: a Python package isn't in the wheelhouse | The change needs a new package. It has to come with a kit built on the internet machine. |
| A setting change has no effect | Restart uvicorn and `run_rfid_tcp`: `.env` is read only at start. |
| Logs of the live system | `sudo journalctl -u backend-web -n 100` (or `backend-tcp`, `backend-ws`, `frontend`). |
