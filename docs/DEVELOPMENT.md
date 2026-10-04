# Developing the backend (Windows)

How to work on the backend on the Windows server: the development copy, its ports and
settings, how to change them, how to test with the door and reader devices, demo data,
tests, and how to put a change live.

Installing the live system: [../deploy/windows/README.md](../deploy/windows/README.md).
Code conventions: [../README.md](../README.md#conventions).

Commands are for the **Command Prompt** unless marked PowerShell. Paths assume the default
installation folder `C:\Management`.

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

The PC runs two separate backends:

| | Live system | Development copy |
|---|---|---|
| What | what the company uses: doors, tills, finance | a developer's working copy, for changes and experiments |
| Code | `C:\Management\backend\current` | `%USERPROFILE%\backend-dev` (a git repository) |
| Settings | `C:\Management\etc\backend.env` | `%USERPROFILE%\backend-dev\.env` (`DEBUG` on) |
| Database | `backend` on PostgreSQL port 5432 (`mgmt-postgres`), backed up | `backend_dev` on its **own** PostgreSQL, port 5433 (`mgmt-postgres-dev`), not backed up |
| Runs | as Windows services (`mgmt-*`), always | only while you start it (section 4) |
| Web app | `C:\Management\frontend\current` | `%USERPROFILE%\frontend-dev` |

Nothing done in the development copy reaches the live data. Its database, demo data and
test runs stay separate. The only things they share are the PC and its ports, so the
development copy uses **different ports** (next section).

## 2. Ports

| What | Live system (service) | Development copy |
|---|---|---|
| Web app and API, for browsers | 443 and 80 (`mgmt-caddy`) | — |
| Backend API | 127.0.0.1:**8000** (`mgmt-web`) | **8100** (uvicorn) |
| Live updates (WebSockets) | 127.0.0.1:8002 (`mgmt-ws`) | 8100, the same port as the API |
| Internal API address for the web app | 127.0.0.1:8001 (`mgmt-caddy`) | — |
| Web app (Next.js) | 127.0.0.1:3100 (`mgmt-frontend`) | 3000 (`npm run dev`) |
| Door and reader devices (TCP) | **9100** (`mgmt-tcp`) | **9101** |
| PostgreSQL | 5432 (`mgmt-postgres`) | 5433 (`mgmt-postgres-dev`) |
| Garnet (Redis-compatible) | 6379, database 0 (`mgmt-garnet`) | the same server, database 1 |

The two differences matter:

- **The development API uses 8100**, because the live API already has 8000. Development
  copies set up before 2026-10-04 used 8000, which pointed `frontend-dev\.env.local` at the
  **live** backend. Re-running `install.cmd` fixes that file. To fix it by hand, see
  section 5.
- **The development door listener uses 9101.** Two programs can't listen on one port, and
  `mgmt-tcp` holds 9100. Copies set up before 2026-10-04 have no `RFID_TCP_PORT` line in
  `.env`: add `RFID_TCP_PORT=9101` yourself.

## 3. Set up a development copy

The installer makes it, once per developer. It needs no internet. In the kit folder:

```
install.cmd -DevUser kim                 :: with the installation
install.cmd -DevUser kim -SeedDemo       :: ... with demo data (section 8)
```

`-DevUser` defaults to the user who installs. `-DevUser none` makes no copies.

What you get, in the user's folder:

- `backend-dev`: the source with its full git history, a Python environment with the
  development tools (pytest, ruff), the database `backend_dev` and a `.env` (DEBUG on).
- `frontend-dev`: the web app's source and `node_modules`. Its `.env.local` points at the
  development backend (`API_URL=http://127.0.0.1:8100`).

Git, Node.js and Python are on the PATH, so open a **new** terminal after installing.
Running `install.cmd` again is safe. It upgrades the live system and never overwrites your
work. A newer kit's code arrives as the git branch `offline/main` (section 10).

Set your name for commits once:

```
git config --global user.name "Kim"
git config --global user.email kim@example.com
```

## 4. Start it

Run these as your normal user, without administrator rights. Use one terminal for each.

**Backend** (API, admin, API docs and WebSockets, reloads on every change):

```
cd %USERPROFILE%\backend-dev
.venv\Scripts\python manage.py createsuperuser
.venv\Scripts\uvicorn config.asgi:application --reload --port 8100
```

You need `createsuperuser` only once, to make a login for this copy. Then open
`http://127.0.0.1:8100/admin/`. The API docs are at `http://127.0.0.1:8100/api/docs/`.

**Door listener** (only when you test with devices, section 7):

```
cd %USERPROFILE%\backend-dev
.venv\Scripts\python manage.py run_rfid_tcp
```

It uses the port from `RFID_TCP_PORT` in `.env`, which is 9101. For a single run on another
port, add `--port 9102`. It doesn't reload: stop it with Ctrl+C and start it again after a
code change.

**Frontend:**

```
cd %USERPROFILE%\frontend-dev
npm run dev
```

Then open `http://127.0.0.1:3000`.

**Background worker:** not needed. The backend has no background jobs yet.

**From another PC** (for example to try the web app on a second screen):

1. Start the servers on all addresses:
   - backend: `uvicorn ... --host 0.0.0.0 --port 8100`
   - frontend: `npm run dev -- -H 0.0.0.0`
2. Add `WS_URL=ws://<this-pc-ip>:8100` to `frontend-dev\.env.local`, so that live updates
   reach the development backend, and restart `npm run dev`.
3. Open the ports for that PC only (administrator Command Prompt):
   ```
   netsh advfirewall firewall add rule name="Dev web" dir=in action=allow protocol=TCP localport=3000,8100 remoteip=<other-pc-ip>
   ```
4. Browse `http://<this-pc-ip>:3000` on the other PC. This is unencrypted, so remove the
   rule when you are done:
   ```
   netsh advfirewall firewall delete rule name="Dev web"
   ```

## 5. Change a port

### Development API port

1. Start uvicorn with the new port: `--port 8200`.
2. Point the development frontend at it, in `frontend-dev\.env.local`:
   ```
   API_URL=http://127.0.0.1:8200
   ```
   If you set `WS_URL` there, change its port too.
3. Restart `npm run dev`. It reads `.env.local` only when it starts.

Never use 8000, 8001, 8002, 3100, 5432 or 6379: the live system has them. On this PC,
`API_URL=http://127.0.0.1:8000` is the **live** backend.

### Development door port

Set it in `backend-dev\.env`, then restart `run_rfid_tcp`:

```
RFID_TCP_PORT=9101
```

For a single run, use `run_rfid_tcp --port N` instead. Then set the same port in the device
simulator or the test device (section 7).

### Development database port

`install.cmd` makes the development PostgreSQL on port 5433 (`$DevPgPort` in
`deploy\windows\setup-dev.ps1`). The copy finds its database through `DATABASE_URL` in
`.env`:

```
DATABASE_URL=postgres://backend_dev:<password>@localhost:5433/backend_dev
```

To move it:

1. Change `port` in `C:\Management\data\pgdata-dev\postgresql.conf`.
2. Restart the service `mgmt-postgres-dev`.
3. Change `DATABASE_URL`.

A later `install.cmd` expects 5433 again, so only do this if 5433 is really taken.

### Live door port

Double-click `C:\Management\change-door-port.bat`, or run `change-door-port.bat 9200`.
It does the following:

- changes `RFID_TCP_PORT` in `backend.env`;
- moves the firewall rule, keeping who may use it;
- restarts `mgmt-tcp` and checks that it answers. If it doesn't come up on the new port, the
  old one is put back.

Then set the devices to the new port. During an installation, `install.cmd -DoorPort 9200`
does the same, and later runs keep the setting.

To change **who** may use the door port, run `install.cmd -DoorNetwork 192.168.100.0/24`.
If you move the live door port, keep the development door port different from the new one.

### Live web ports

Use `install.cmd -HttpPort 8080 -HttpsPort 8443`. The setting is **not remembered**: pass
the same options on every later run of `install.cmd`, or it goes back to 80/443.

The internal ports (8000, 8001, 8002, 3100) are fixed by the installer. Only the PC itself
can reach them, so there is no need to change them.

## 6. Settings (.env)

`backend-dev\.env` is made from [`.env.example`](../.env.example). It is read at start, so
restart uvicorn or `run_rfid_tcp` after a change. These are the settings you are most likely
to change:

| Setting | Development copy | What it does |
|---|---|---|
| `DATABASE_URL` | `backend_dev` on port 5433 | the database |
| `REDIS_URL` | `redis://localhost:6379/1` | cache and live updates (database 1; the live system uses 0) |
| `RFID_TCP_PORT` | `9101` | the door listener's port |
| `RFID_TCP_HOST` | `0.0.0.0` | `127.0.0.1` = only from this PC |
| `TEST_CONSOLE_ENABLED` | off | `true` turns on `/test-console/` (door scans and till without devices) |
| `TIME_ZONE`, `LANGUAGE_CODE`, `DEVICE_LANGUAGE` | copied from the live system | |
| `ATTENDANCE_DIRECTION_RULE` | `device` | in/out from the door unit; run `manage.py rebuild_attendance` after a change |
| `RFID_DEBOUNCE_SECONDS` | 10 | a second tap of the same card within this time is a duplicate |
| `PURCHASE_PIN_MAX_ATTEMPTS`, `PURCHASE_PIN_LOCKOUT_MINUTES` | 5, 15 | PIN lockout |
| `FINANCE_MAX_DEPOSIT` | 1000.00 | largest single deposit |
| `LOG_LEVEL` | `INFO` | `DEBUG` for more detail |

The full list, with defaults, is in [`config/settings/base.py`](../config/settings/base.py).
Tests don't use your `.env`, except for finding the database: `config/settings/test.py`
fixes the rest.

## 7. Test with door and reader devices

The devices speak raw TCP to the door listener (protocol:
[DEVICE_INTEGRATION.md](DEVICE_INTEGRATION.md)). Test the development copy with
`DeviceSimulator.exe` (or `tools\device-simulator\device_simulator.py`), or with a spare
real device:

1. Start the development door listener (section 4). It listens on 9101.
2. Point the simulator at this PC and **port 9101**. In its window, set server and port. On
   the command line:
   ```
   DeviceSimulator.exe --tap Reader1 DC62B3E3 --server 127.0.0.1 --port 9101
   ```
   Make sure it isn't set to 9100: that is the live system.
3. The **development** database must know the device. Register it under **Readers → New
   device** in the development web app, or use the demo data:
   - **Till readers** (`Reader1`) and **card assign readers** (`Master1`) are recognised by
     their ID alone, from any address.
   - **Door units** are recognised by their ID **and the address they send from**.
     `Door1-1` must come from 192.168.100.151.
     - Simulator on this PC with server `127.0.0.1`: register a door unit with IP
       `127.0.0.1`.
     - Simulator on another PC: use that PC's IP, or give that PC the door addresses (see
       the simulator's README).
4. If the simulator runs on another PC, open 9101 for that PC only, and remove the rule when
   you are done (administrator Command Prompt):
   ```
   netsh advfirewall firewall add rule name="Dev door listener" dir=in action=allow protocol=TCP localport=9101 remoteip=<other-pc-ip>
   netsh advfirewall firewall delete rule name="Dev door listener"
   ```

Every packet and every answer appears in the development web app's **TCP log** page
(admins) and in the listener's window. A device that gets `CARD_NO` is explained there.

**Without devices:** set `TEST_CONSOLE_ENABLED=true` in `.env`, restart uvicorn and open
`http://127.0.0.1:8100/test-console/`. It simulates door scans and the till through the
normal pipeline. It skips only the TCP connection. Never set this in
`C:\Management\etc\backend.env`.

## 8. Demo data: add, remove

Demo data exists only in the development copy. The `seed_*` commands refuse to run when
`DEBUG` is off, which is always the case on the live system.

**Add** it with `install.cmd -SeedDemo` (section 3), or at any time. Run the commands in
this order:

```
cd %USERPROFILE%\backend-dev
.venv\Scripts\python manage.py seed_demo
.venv\Scripts\python manage.py seed_more
.venv\Scripts\python manage.py seed_attendance_month
.venv\Scripts\python manage.py seed_purchases_month
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

**Remove** it by emptying the development database. There is no "unseed" command: demo money
and stock are in append-only ledgers.

1. Stop uvicorn and `run_rfid_tcp`: a database that is in use can't be dropped.
2. In an administrator PowerShell:
   ```powershell
   $env:PGPASSFILE = 'C:\Management\etc\private\pgpass.conf'
   & 'C:\Management\runtime\pgsql\bin\dropdb.exe' -h localhost -p 5433 -U postgres backend_dev
   & 'C:\Management\runtime\pgsql\bin\createdb.exe' -h localhost -p 5433 -U postgres -O backend_dev backend_dev
   ```
3. As yourself:
   ```
   cd %USERPROFILE%\backend-dev
   .venv\Scripts\python manage.py migrate
   .venv\Scripts\python manage.py createsuperuser
   ```

## 9. Make a change

```
cd %USERPROFILE%\backend-dev
git switch -c my-change
.venv\Scripts\pytest -q
.venv\Scripts\pytest -q tests\test_tcp_log.py
.venv\Scripts\ruff check .
.venv\Scripts\ruff format .
git add -A
git commit -m "Describe the change"
```

- `git switch -c` makes a branch for the change. It is optional.
- `pytest -q` runs all tests, which takes a few minutes. Give it a file to run only that
  file.
- The tests make their own throw-away database (`test_backend_dev`) and never touch
  `backend_dev`.

**Model changes:**

```
.venv\Scripts\python manage.py makemigrations <app>
.venv\Scripts\python manage.py migrate
```

`makemigrations` writes `apps\<app>\migrations\00xx_...`, and `migrate` applies it to the
development database. Read the new migration before committing. If `makemigrations` asks a
question (a default for a new field), answer it, or give the field a default in the model.
Test migrations here first: switching back to an older release doesn't undo them on the live
system.

**Where things go** (details in [../README.md](../README.md#conventions)):

- Business rules go in `apps\<app>\services.py`, not in views or serializers. Errors are
  `DomainError` subclasses with a stable `code`.
- New permissions go in `apps\accounts\rbac.py`. `migrate` adds them, and only **new**
  permissions are granted to roles. `manage.py sync_rbac` runs the same step by itself.
- Ledgers and logs are append-only (database triggers). Correct them with a new entry, never
  by editing.
- User-facing texts use `_("...")`, with a Korean translation in `locale\ko_KP`
  ([../README.md](../README.md#translations-english--korean-dprk-usage)).
  `tests\test_i18n.py` fails when one is missing.
- The door protocol lives in `apps\rfid\tcp.py`, and device lookup by ID and IP in
  `apps\rfid\authentication.py`.

**Checks** for when something looks wrong in the data:

| Command | What it does |
|---|---|
| `.venv\Scripts\python manage.py check_ledger` | checks developer balances against their ledger |
| `.venv\Scripts\python manage.py check_inventory` | checks stock against the movements |
| `.venv\Scripts\python manage.py rebuild_attendance` | rebuilds attendance from the door scans |

## 10. Put your change live

Only **committed** changes go live, so commit first. Then double-click one of these in
`C:\Management` (they ask for administrator rights):

| File | What it puts live |
|---|---|
| `deploy-backend.bat` | `backend-dev`: safety backup, new release, database migrations, static files, restart |
| `deploy-frontend.bat` | `frontend-dev`: builds it (a few minutes), restart |
| `deploy-all.bat` | both |

If the new release doesn't start, the previous one is put back and the window says why. A
change that needs a **new Python package** must find it in `backend-dev\.offline-cache\wheelhouse`.
Otherwise it has to come with a kit built on the internet machine.

**Send your commits to the internet machine** (USB stick, e.g. drive `E:`), or the next kit
won't have them:

```
cd %USERPROFILE%\backend-dev
git bundle create E:\backend-changes.bundle offline/main..main
```

On the internet machine, run `git fetch E:/backend-changes.bundle main:offline-server`,
then review and merge.

**When a newer kit arrives**, run its `install.cmd`. Your copy gets the new code as the
branch `offline/main`. Merge it when you're ready:

```
cd %USERPROFILE%\backend-dev
git merge offline/main
.venv\Scripts\python manage.py migrate
```

## 11. Problems

| Symptom | Cause and fix |
|---|---|
| `error 10048` / `address already in use` starting uvicorn or `run_rfid_tcp` | Another program has the port, often the live system (section 2). Use 8100 and 9101. To find the program: `netstat -ano \| findstr :9101`, then look up the PID in Task Manager. |
| The development web app shows live data | `frontend-dev\.env.local` says `API_URL=http://127.0.0.1:8000`, which is the live API. Set 8100 and restart `npm run dev`. |
| The simulator gets `CARD_NO` | Read the reason in the TCP log or the listener's window. Either the device isn't registered in the **development** database, a door unit sends from another IP, or you sent to 9100 (live) instead of 9101. |
| The simulator gets no answer at all | The development listener isn't running, or the firewall blocks 9101 (simulator on another PC). |
| `seed_demo only runs with DEBUG=True` | You are in the live system's folder. Run it in `backend-dev`. |
| `Demo data already exists.` | Already seeded. To start again, empty the database (section 8). |
| `could not connect to server ... port 5433` | The development PostgreSQL isn't running: start the service `mgmt-postgres-dev` (`services.msc`). |
| A setting change has no effect | Restart uvicorn and `run_rfid_tcp`: `.env` is read only at start. |
| Logs of the live system | `C:\Management\logs\` (`mgmt-web.out.log`, `mgmt-tcp.out.log`, ...). |
