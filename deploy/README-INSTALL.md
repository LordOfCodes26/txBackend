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
- **Network:** the server's fixed IP address in the company network. Clients reach it on
  port **443** (HTTPS, also 80); door devices send to TCP port **9100**.
- **Decide before installing:**
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
Default language for the web and API: en = English, ko = Korean [en]:
Language on door/till reader screens (ko only if they show Korean letters): en / ko [en]:
Admin email [admin@example.com]:
Password: / Password (again):
```

Or give the answers up front:

```bash
sudo bash install-backend.sh --timezone Asia/Pyongyang --language ko \
     --device-language en --admin-email admin@chonha.com
```

| Option | Meaning |
|---|---|
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

If a check fails, see **8. Problems**.

## 4. Check it works

From a computer in the company network, open `https://<server-ip>/admin/` and sign in
with the admin account. The browser warns about the certificate the first time: the
installer made a temporary self-signed one (see step 5).

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

4. **Buildings and devices:** create the buildings (Building 1, Building 2), register the
   doors (`Door1`, `Door2`) with each door's fixed IP, and the till readers with their
   serial numbers. A door that isn't registered yet shows up in the log as
   `rejected ID='Door1' from <ip>`:

   ```bash
   sudo journalctl -u backend-tcp -f
   ```

5. **Backups to a second place:** backups are made every night into
   `/var/backups/backend`, on the same disk. Set a second disk or machine in
   `/etc/backend/backup.conf` (`OFFSITE_DIR=` or `OFFSITE_RSYNC=`).

6. **Firewall:** allow 443 (and 80) from the company network, and 9100 only from the
   door devices.

## 6. Upgrading to a newer version

Copy the new `backend-<new version>.tar.gz`, its `.sha256` and `install-backend.sh` into
the same folder, and run the same command:

```bash
sudo bash install-backend.sh
```

It takes the newest bundle in the folder, **makes a safety backup first**, then upgrades.
Data, accounts and settings are kept; the questions above are not asked again (pass an
option, e.g. `--language ko`, to change a setting).

## 7. Where things are

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

## 8. Problems

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
