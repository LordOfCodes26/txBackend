# Management system: offline installation on Windows

This kit installs the whole management system on **one Windows 10/11 PC without internet**:
the web app, the backend, the database, nightly backups, and development copies of the code.
Everything it needs is in this folder.

Browsers, till programs and door devices in the company network then use this PC as their
server, at one address: `https://<this PC's IP>/`.

## 1. What you need

| | |
|---|---|
| Windows | **Windows 10 (version 1809 or newer) or Windows 11**, 64-bit, Pro edition recommended |
| Account | a Windows account with **administrator** rights |
| Disk | **15 GB free** on drive C: (the system and its backups live in `C:\Management`) |
| Memory | 8 GB or more |
| Network | a **fixed IP address** for this PC (section 3) |
| Ports | **80, 443** (web) and **9100** (doors) free: no IIS, no other web server on this PC |

The PC must stay **switched on**: the installer turns off sleep and hibernation while it is
on mains power. Use a PC that doesn't run anything else important.

## 2. Copy the kit

1. Copy `windows-kit-<version>.zip` (and its `.sha256` file) to the PC, e.g. with a USB stick.
2. Right-click the zip, **Extract All...**, and extract to a short path such as `C:\Kits\`.
   Not the Desktop, not OneDrive, not a network drive.

You get a folder `C:\Kits\windows-kit-<version>` with `install.cmd`, this `README.md`,
`runtime\` and two large `.tar.gz` files. Don't take files out of it: the installer checks
every file against `SHA256SUMS` and stops if one is missing or damaged.

## 3. Prepare the PC (once)

- **Fixed IP address.** Settings > Network & Internet > (Ethernet) > IP assignment > **Edit**
  > Manual: IPv4 on, e.g. IP `192.168.1.10`, subnet prefix `24`, your gateway and DNS. Ask the
  network administrator which address to use. Doors are set up with this address, so it must
  never change.
- **Name.** Settings > System > About > Rename this PC, e.g. `MGMT-SERVER` (optional).
- **Clock and timezone.** Settings > Time & language. Attendance and purchases are recorded
  with this clock, so it must be right. Without internet, set it by hand or point Windows at
  the company's time server (Control Panel > Date and Time > Internet Time).
- **Windows Update.** Offline it does nothing; if the PC is ever online, set active hours so
  it doesn't restart during working time.

## 4. Install

Double-click **`install.cmd`** and answer **Yes** when Windows asks for administrator rights.
The installer asks a few questions (Enter keeps the suggestion in brackets):

| Question | Answer |
|---|---|
| Continue? | `yes` |
| Company timezone | e.g. `Asia/Pyongyang` (a name like Area/City) |
| Default language for the web and API | `en` (English) or `ko` (Korean) |
| Language on door/till reader screens | `ko` only if the readers can show Korean letters |
| IP address that computers, tills and doors will use | the fixed IP from section 3 |
| Admin username, password | the first administrator account of the web app |

It then works for **10 to 30 minutes** (the antivirus checks tens of thousands of files):
it unpacks the programs, creates the database, builds the web app, starts the services,
opens the firewall, makes a first backup and checks everything.

### Success looks like this

```
==> 12. Checks
    mgmt-postgres                Running
    mgmt-garnet                  Running
    mgmt-web                     Running
    ...
    frontend                     200  https://localhost/
    backend                      200  https://localhost/health/
    backups                      200  https://localhost/health/backup/
    door listener :9100          open

Everything is installed.
```

If a step fails, the installer stops with a red **ERROR** and the reason. Fix it and run
`install.cmd` again: it continues where things stand and never loses data.

### Options

Instead of answering questions, give everything at once (in an administrator PowerShell in
the kit folder):

```powershell
.\install.cmd -ServerIp 192.168.1.10 -TimeZone Asia/Pyongyang -Language ko -AdminUser admin -DevUser kim
```

| Option | Meaning |
|---|---|
| `-ServerIp IP` | the PC's fixed IP |
| `-TimeZone Area/City` | company timezone |
| `-Language en\|ko`, `-DeviceLanguage en\|ko` | web/API language, reader screen language |
| `-AdminUser NAME`, `-NoAdmin` | the first admin account, or none |
| `-DevUser NAME` | whose development copies (default: you; `none` = no copies) |
| `-SeedDemo` | fill the development copy with demo data |
| `-DoorNetwork 192.168.1.0/24` | only these addresses may use the door port (kept on later runs) |
| `-DoorPort 9200` | the TCP port the devices send to (default 9100; see section 7 to change it later) |
| `-Hosts "name1,name2"` | extra names/IPs clients use to reach this PC |
| `-Root D:\Management` | install somewhere else than `C:\Management` |
| `-Yes` | ask nothing (with `DJANGO_SUPERUSER_PASSWORD` set, also creates the admin) |

## 5. Open it in a browser

On this PC: <https://localhost/>. From another PC: `https://192.168.1.10/` (your IP).

- **Web app:** `https://<ip>/` (sign in with the admin account)
- **Backend admin:** `https://<ip>/admin/`
- **API documentation:** `https://<ip>/api/docs/` (sign in at `/admin/` first)

### The certificate (once per PC)

The connection is encrypted with the system's **own certificate**. This PC already trusts it.
**Other PCs show a warning** until you install the certificate on them once:

1. Copy `C:\Management\certificate\management-root-ca.crt` to the other PC (USB stick).
2. Double-click it > **Install Certificate...** > **Local Machine** > Next >
   **Place all certificates in the following store** > Browse > **Trusted Root
   Certification Authorities** > OK > Next > Finish.
3. Restart the browser. `https://<ip>/` now opens without a warning.

The certificate is valid for 10 years. Till programs need it too (section 9).

If the company has its own certificate (from its certificate authority), put it in
`C:\Management\etc\tls\cert.pem` and its key in `C:\Management\etc\tls\key.pem` (PEM format),
then run `install.cmd` again: it's used instead.

## 6. After installing

1. **Off-site backups.** Backups are on this PC's disk; a disk failure takes them too. Open
   `C:\Management\etc\backup.conf` in Notepad (as administrator) and set `OFFSITE_DIR` to a
   folder on **another drive or PC**, e.g. `OFFSITE_DIR=D:/Backups/management` or
   `OFFSITE_DIR=//nas/backups/management`. The next nightly backup copies everything there.
2. **Buildings, doors, tills, people:** sections 8 and 9.
3. **Firewall:** the installer opened 80, 443 and 9100. To allow the door port only from the
   doors' network, run `install.cmd -DoorNetwork 192.168.1.0/24`.

## 7. Everyday operation

Everything runs as **Windows services** that start with Windows and restart themselves after
a failure. See them in the Services app (`services.msc`); their names start with `mgmt-`.

| Service | What it is |
|---|---|
| `mgmt-caddy` | the web server: HTTPS on 80/443, one address for everything |
| `mgmt-frontend` | the web app (Next.js), 127.0.0.1:3100 |
| `mgmt-web` | the backend API (waitress), 127.0.0.1:8000 |
| `mgmt-ws` | live updates (WebSockets), 127.0.0.1:8002 |
| `mgmt-tcp` | the door listener, TCP **9100** (or the port you chose) |
| `mgmt-postgres` | the database (PostgreSQL 16), localhost:5432 |
| `mgmt-garnet` | cache and live-update messages (Redis-compatible), localhost:6379 |
| `mgmt-postgres-dev` | the development database, localhost:5433 (with development copies) |

**Restart everything:** in an administrator PowerShell:
`Get-Service mgmt-* | Restart-Service -Force`

**Logs:** `C:\Management\logs\` (`mgmt-web.out.log`, `mgmt-tcp.out.log`, ...; the database's
in `C:\Management\data\pgdata\log\`).

**Health:** `https://<ip>/health/`, `/health/db/`, `/health/redis/`, `/health/backup/`
(all answer `"status": "ok"`).

**Management commands** (in an administrator command prompt):
`C:\Management\manage.cmd <command>`, e.g. `C:\Management\manage.cmd changepassword admin`
or `C:\Management\manage.cmd createsuperuser`.

### Changing the door port

The doors and readers send to TCP **9100**. To use another port (e.g. 9200), in an
administrator command prompt:

```
C:\Management\change-door-port.bat 9200
```

(or double-click it and type the port). It changes `RFID_TCP_PORT` in `backend.env`, moves
the firewall rule (who may use the port stays the same), restarts the door listener and
checks it answers on the new port; if it doesn't, the old port is put back. Then **set every
door and reader device to the new port**: until then they aren't heard.

The port is a setting, not code: changing it in the backend's code and deploying with
`deploy-backend.bat` changes nothing (`backend.env` wins). Upgrades keep the port you chose.

## 8. Doors (attendance)

All devices (doors, till readers, card assign readers) send to **TCP 9100** on this PC. Each
tap is one packet between `$` signs, answered with `CARD_OK`, `CARD_NO` or (doors)
`CARD_DENIED`. The listener **rejects every device until it is registered**.

A door usually has **several units** (readers) that all send the same ID, e.g. Door1-1 …
Door1-4 at 192.168.100.151 … .154, all sending `$ID:Door1,TYPE:Input,UID=...$` (or
`TYPE:Output` on the way out). Each unit is registered as **its own device**: the same code,
its own name and its own fixed IP.

1. In the web app, **Buildings > New**: create each building.
2. Set each door unit (its own settings screen or tool) to send to **`<this PC's IP>`, port
   `9100`**, with its door ID (`Door1`, ...). Give each unit a fixed IP.
3. **Readers > New** for every unit: kind **Attendance door**, code `Door1` (exactly the ID
   the unit sends), name `Door1-1`, its building and **allowed IP** `192.168.100.151`.
4. Tap a card: an unregistered card gets `CARD_NO`, so the connection works. A unit that
   isn't registered with its IP shows up in `C:\Management\logs\mgmt-tcp.out.log` as
   `rejected ID='Door1' from 192.168.100.155`: that's the address the unit really uses.
5. **Cards > Assign card** (with a card assign reader, section 9) or **Cards > New**, then
   assign each card to its developer and their building.
6. Tap again: the unit answers `CARD_OK` (the door opens), and **Occupancy** in the web app
   updates live. A registered card that may not enter (not assigned, blocked, developer not
   active) gets `CARD_DENIED`.

The full device protocol is in `C:\Management\backend\current\docs\DEVICE_INTEGRATION.md`.

## 9. Till readers and card assign readers

These readers also send to **TCP 9100** and are recognised by their **ID alone** (each ID
once), so they can move to another PC.

1. **Readers > New**: code `Reader1` (exactly the ID the reader sends), kind **Till reader**
   and its **Seller** (a seller can have several readers); code `Master1`, kind **Card assign
   reader**.
2. Point each reader at **`<this PC's IP>`, port `9100`**. They send
   `$ID:Reader1,TYPE:Pay,UID=...$` and `$ID:Master1,TYPE:Master,UID=...$`.
3. A payment: the seller opens the till page, picks the counter (the seller's reader is
   used), adds goods, clicks **Scan card to buy**; within 2 minutes the developer taps (the
   reader gets `CARD_OK`) and types their PIN; the seller confirms. A tap with no purchase
   waiting, or a card that can't pay, gets `CARD_NO`.
4. A card assign reader: in the web app, **Cards > New** or **Cards > Assign card**, pick the
   reader and tap the card. A new card gets `CARD_NO` and is registered by the tap; a known
   card gets `CARD_OK`.

## 10. Backups and restoring

- **Every night at 02:30** the scheduled task **"Management nightly backup"** dumps the
  database and uploaded files to `C:\Management\backups\`, **restores the dump into a scratch
  database to prove it works** (ledgers and stock must add up), deletes backups older than 14
  days and copies everything to `OFFSITE_DIR`.
- **A backup now:** Task Scheduler > "Management nightly backup" > Run, or in an administrator
  PowerShell: `Start-ScheduledTask "Management nightly backup"`. Its log:
  `C:\Management\logs\backup.log`.
- **Is it working?** `https://<ip>/health/backup/` says `"status": "ok"`; otherwise it says
  what's wrong (too old, failed check, ...).

**Restore** the live database from a backup (in an administrator PowerShell):

```powershell
cd C:\Management\backend\current\deploy\windows
.\restore.ps1 -Dump C:\Management\backups\db\backend-20261003-023000.dump -Yes
```

It first checks the dump restores cleanly, then stops the backend, **keeps the current
database under a new name** (never deleted), restores, and starts the backend again.
Uploaded files are in `C:\Management\backups\media\media-<date>.tar.gz`
(`tar -xzf <file> -C C:\Management\data\media`).

## 11. A newer kit (upgrades)

Extract the new kit next to the old one and run its `install.cmd`. It:

1. makes a **safety backup** first (and stops if that fails),
2. installs the new version next to the current one and switches over (the last three
   versions are kept in `C:\Management\backend\releases\`),
3. keeps all data, settings, accounts and certificates,
4. **never overwrites developers' work** (section 12).

## 12. Development on this PC

With `-DevUser` (default: the user who installs), each developer gets, in their user folder:

| Folder | What | Start it |
|---|---|---|
| `backend-dev` | the backend source, git history, Python environment with tests and tools, **own database** (port 5433) | `.venv\Scripts\uvicorn config.asgi:application --reload --port 8100` (8000 is the live API) |
| `frontend-dev` | the frontend source, git history, `node_modules` | `npm run dev` (http://127.0.0.1:3000, uses the development backend) |

Git, Node.js and Python are on the PATH (open a **new** terminal after installing).
Full development guide (ports, settings, testing with devices, demo data): `backend-dev\docs\DEVELOPMENT.md`.
The development door listener (`manage.py run_rfid_tcp`) uses port 9101, not the live 9100.
Useful commands in `backend-dev`: `.venv\Scripts\python manage.py createsuperuser` (a login
for the copy), `.venv\Scripts\pytest -q` (tests), `.venv\Scripts\ruff check .` (lint).

**A newer kit** never changes your copies: its code arrives as the git branch
**`offline/main`**. Merge it when you're ready:

```
cd %USERPROFILE%\backend-dev
git merge offline/main
.venv\Scripts\python manage.py migrate
cd %USERPROFILE%\frontend-dev
git merge offline/main
rmdir /s /q node_modules & robocopy .offline-cache\node_modules node_modules /E /NFL /NDL /NJH /NJS & npm rebuild --ignore-scripts --offline
```

### Putting your changes live

1. **Commit** your changes in the copy (only committed changes are deployed):
   `git add -A` then `git commit -m "what changed"`.
2. Double-click one of these in `C:\Management` (they ask for administrator rights):

| File | What it puts live |
|---|---|
| `deploy-backend.bat` | `backend-dev`: safety backup, new release, database migrations, static files, restart |
| `deploy-frontend.bat` | `frontend-dev`: builds it (a few minutes), restart |
| `deploy-all.bat` | both |

Each deploy is a **new release next to the running one** (the last three are kept). If the
new one doesn't start, the previous one is put back automatically and the window says why.
Database migrations can't be undone that way: test them in `backend-dev` first
(`.venv\Scripts\python manage.py migrate` on its own database, then `pytest`).

Limits (offline): a **new Python package** must be in the copy's
`.offline-cache\wheelhouse` and a **new npm package** in `frontend-dev\node_modules`;
otherwise it has to come with a kit built on the internet machine. Send your commits
there too (below), or the next kit will not have them.

**Sending your changes to the internet machine** (USB stick, e.g. drive `E:`):

```
cd %USERPROFILE%\backend-dev
git bundle create E:\backend-changes.bundle offline/main..main
cd %USERPROFILE%\frontend-dev
git bundle create E:\frontend-changes.bundle offline/main..main
```

On the internet machine: `git fetch E:/backend-changes.bundle main:offline-server`, look at
the commits, merge, test, and build the next kit (`scripts/build_windows_kit.sh`).

## 13. Where things are

| Path | What |
|---|---|
| `C:\Management\backend\current` | the running backend (a link to `releases\<version>`) |
| `C:\Management\frontend\current` | the running web app |
| `C:\Management\etc\backend.env` | **settings** (administrators only); restart the services after changes |
| `C:\Management\etc\backup.conf` | backup settings (`OFFSITE_DIR`, `KEEP_DAILY_DAYS`) |
| `C:\Management\etc\Caddyfile` | web server configuration (rewritten by the installer) |
| `C:\Management\etc\private\` | database administrator passwords (administrators only) |
| `C:\Management\data\` | database files, uploaded files, certificates |
| `C:\Management\backups\` | nightly backups |
| `C:\Management\logs\` | logs of every service and of the backups |
| `C:\Management\certificate\` | the certificate for the other PCs |
| `C:\Management\runtime\` | Python, Node.js, PostgreSQL, Garnet, Caddy, ... |

## 14. Problems

| What you see | Meaning / fix |
|---|---|
| `Port 443 is used by 'System'` | IIS or another web server: turn it off (Windows Features > Internet Information Services), or install with `-HttpPort 8080 -HttpsPort 8443` |
| `Port 5432 is used by 'postgres'` (not ours) | another PostgreSQL on the PC: stop it, or install with `-PgPort 5434` |
| `The service mgmt-... did not start` | the installer prints the end of its log; all logs are in `C:\Management\logs\` |
| Browser warning on another PC | install the certificate on that PC (section 5) |
| Other PCs can't open the page at all | wrong IP, or another firewall (antivirus) blocks 443: check `ping <ip>`, and allow 443 in the antivirus |
| A door is rejected | its ID or IP doesn't match its registration (section 8; the log says which) |
| Times are off by hours | wrong timezone: `install.cmd -TimeZone Asia/Pyongyang` |
| Forgot the admin password | `C:\Management\manage.cmd changepassword <username>` (administrator command prompt) |
| `/health/backup/` not ok | `C:\Management\logs\backup.log`; run a backup now (section 10) |

## 15. Removing it

Double-click **`uninstall.bat`**: in `C:\Management`, or in the kit folder. It asks for
administrator rights and then for confirmation (type `yes`). It removes the services,
firewall rules and backup task, and **keeps** the data, settings and backups, so running
`install.cmd` again brings everything back.

To remove **everything** in `C:\Management` too (database, uploaded files, backups), from a
command prompt:

```
C:\Management\uninstall.bat -RemoveData
```

Copy the backups (`C:\Management\backups`) somewhere else first if you might need them.

Developers' `backend-dev` and `frontend-dev` folders are never removed.
