# Installing the backend on the offline server

This folder contains everything needed. The server does **not** need internet access.

| File | What it is |
|---|---|
| `backend-<version>.tar.gz` | The bundle: application, Python packages and Ubuntu packages |
| `backend-<version>.tar.gz.sha256` | Checksum, to detect a damaged copy |
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
ls        # backend-<version>.tar.gz  backend-<version>.tar.gz.sha256  install-backend.sh
```

## 3. Install

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

If a check fails, see **9. Problems**.

## 4. Check it works

From a computer in the company network, open `https://<server-ip>/admin/` and sign in
with the admin account. The browser warns about the certificate the first time: the
installer made a temporary one for the server IP (see step 5).

## 5. After installing

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

4. **Buildings and doors:** see **6. Connecting the door devices**, step by step.
   Till readers are registered with their serial numbers (`sn`).

5. **Backups to a second place:** backups are made every night into
   `/var/backups/backend`, on the same disk. Set a second disk or machine in
   `/etc/backend/backup.conf` (`OFFSITE_DIR=` or `OFFSITE_RSYNC=`).

6. **Firewall:** allow 443 (and 80) from the company network, and 9100 only from the
   door devices.

## 6. Connecting the door devices (attendance)

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

## 7. Upgrading to a newer version

Copy the new `backend-<new version>.tar.gz`, its `.sha256` and `install-backend.sh` into
the same folder, and run the same command:

```bash
sudo bash install-backend.sh
```

It takes the newest bundle in the folder, **makes a safety backup first**, then upgrades.
Data, accounts and settings are kept; the questions above are not asked again (pass an
option, e.g. `--language ko`, to change a setting).

## 8. Where things are

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

## 9. Problems

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
