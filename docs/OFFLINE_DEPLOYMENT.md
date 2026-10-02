# Deploying to the Offline Server

The production server has no internet access, so everything travels in **one bundle file**:
the application, every Python package, and the OS packages (Python, PostgreSQL 16, Redis,
nginx, …) with their dependencies.

## 0. Requirements

- The offline server must run **Ubuntu 24.04 (x86_64)**, the same as the build machine.
  Check on the offline server: `lsb_release -ds; uname -m`. (The installer refuses any other
  OS; for another version, build the bundle on a machine running that version.)
- About 2 GB free disk for the system, plus space for data and backups.
- Root access (sudo) on the offline server.

## 1. Build the bundle (on a machine with internet)

```bash
cd /root/backend
git status                       # must be clean: the bundle contains only committed code
scripts/build_offline_bundle.sh 2026.10.02
sha256sum dist/backend-2026.10.02.tar.gz > dist/backend-2026.10.02.tar.gz.sha256
```

Result: `dist/backend-<version>.tar.gz` (~140 MB) and its checksum. The bundle never
contains `.env` files, keys or passwords; each server generates its own at install.

## 2. Copy it to the offline server

Copy the three files from `dist/` into one folder on the offline server (USB disk or
internal network): `backend-<version>.tar.gz`, its `.sha256` and `install-backend.sh`.
Then the easy way, which checks, unpacks (`tar -xzf`) and installs in one go:

```bash
sudo bash install-backend.sh                        # newest backend-*.tar.gz in this folder
```

On a first install it asks for the company timezone and the **languages** (English or
Korean): the default for the web/API, and the one on the door and till reader screens
(choose Korean for readers only if their screens can show Korean letters). Change them
later, or set them without questions:

```bash
sudo bash install-backend.sh --language ko --device-language en --timezone Asia/Pyongyang
```

On an upgrade the current settings are kept unless you pass these options.

To only unpack (no installation), e.g. to look inside first:

```bash
bash install-backend.sh --extract-only              # unpacks next to the script
```

Or by hand:

```bash
sha256sum -c backend-2026.10.02.tar.gz.sha256      # must say OK
tar -xzf backend-2026.10.02.tar.gz
cd backend-2026.10.02
sudo app/scripts/install_offline.sh
```

The installer (about 2 minutes) does all of this, without internet:

1. checks the OS matches the bundle;
2. installs the OS packages from the bundle;
3. creates the `backend` service user, `/etc/backend/backend.env` (random secret key and
   database password), the PostgreSQL database and a **self-signed** TLS certificate;
4. installs the application to `/opt/backend/releases/<version>`, applies database
   migrations, collects static files and switches `/opt/backend/current`;
5. installs and starts the services: `backend-web` (API), `backend-ws` (live updates),
   `backend-tcp` (door devices, TCP 9100), `backend-worker`, nginx;
6. enables backups: WAL archiving, nightly and weekly timers, a first base backup and a
   first verified dump.

Check: `curl -k https://localhost/health/`, `/health/db/`, `/health/redis/`, `/health/backup/`
should all return 200.

## 3. After the first install

1. **Create the first admin user:**
   ```bash
   cd /opt/backend/current
   sudo -u backend bash -c 'set -a; . /etc/backend/backend.env; set +a; .venv/bin/python manage.py createsuperuser'
   ```
2. **TLS certificate:** replace `/etc/backend/tls/cert.pem` and `key.pem` with a certificate
   from your internal certificate authority for the server's name or IP, then
   `sudo systemctl reload nginx`. (Otherwise browsers warn about the self-signed certificate.)
3. **Company timezone and allowed hosts:** in `/etc/backend/backend.env` set `TIME_ZONE`
   (e.g. `Asia/Seoul`) and check `DJANGO_ALLOWED_HOSTS` (the server's LAN IP/name), then
   `sudo systemctl restart backend-web backend-ws backend-tcp`.
4. **Off-site backups:** set `OFFSITE_DIR` or `OFFSITE_RSYNC` in `/etc/backend/backup.conf`
   (a second disk, NAS or another machine). See `BACKUP_AND_RESTORE.md`.
5. **Devices** (see `DEVICE_INTEGRATION.md`):
   - Buildings: create `Building 1` / `Building 2`.
   - Doors: register `Door1` / `Door2` with their building, point them at
     `<server-ip>:9100`, tap a card, read each door's IP from
     `journalctl -u backend-tcp` (`rejected ID='Door1' from …`) and set it as `allowed_ip`.
   - Till readers: register `Reader1`, `Reader2`, … with their serial number (`sn`); the
     till program on each seller's PC sends `{"SN","ID","TYPE":"pay","UID"}` and a heartbeat
     every 30 s to `https://<server-ip>/api/v1/rfid/events/`.
6. **Firewall:** allow only what's needed, e.g. 22 (admin), 443 (and 80 for the redirect),
   and 9100 from the doors' addresses.
7. **Data:** create roles' users, developers (or import them), sellers and goods. The demo
   data commands (`seed_*`) only run in development and must not be used here.

## 4. Upgrades

Build a new bundle with a new version, copy it over and run its installer the same way. It
keeps the database and settings, installs the new release next to the old ones, applies
migrations and restarts the services. Tested: data recorded before an upgrade is intact
afterwards.

**Roll back** (only if the new release added no database migrations you need to undo):
```bash
sudo ln -sfn /opt/backend/releases/<previous-version> /opt/backend/current
sudo systemctl restart backend-web backend-ws backend-tcp backend-worker
```
Take a backup before upgrading: `sudo systemctl start backend-backup`.

## 5. Not included

- The **frontend** (Next.js) and its Node.js runtime: the frontend team builds it with
  `output: "standalone"` and adds it to the server separately (see `FRONTEND_README.md`).

## Verified (2026-10-01)

On a freshly built, minimal Ubuntu 24.04 container with networking disabled (no
interfaces but loopback): the installer completed in about 2 minutes, all services were
active and all health checks returned 200. An upgrade from an older bundle on the same
machine kept existing data and the previous release for rollback.
