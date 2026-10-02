# Deploying to the Offline Server

The production server has no internet access, so everything travels in **one bundle file**:
the application, every Python package, and the OS packages (Python, PostgreSQL 16, Redis,
nginx, …) with their dependencies.

This document is for whoever builds and maintains the deployment. The person who installs
the server only needs `README-INSTALL.md`, which ships next to the bundle.

## 0. Requirements

- The offline server must run **Ubuntu 24.04 (x86_64)**, the same as the build machine.
  Check on the offline server: `lsb_release -ds; uname -m`. (The installer refuses any other
  OS; for another version, build the bundle on a machine running that version.)
- At least 10 GB free disk (system, database and its backups).
- Root access (sudo) on the offline server.

## 1. Build the bundle (on a machine with internet)

```bash
cd /root/backend
git status                                   # must be clean: only committed code is bundled
scripts/build_offline_bundle.sh 2026.10.05   # the version is the bundle's name
```

Result in `dist/`, the four files to copy:

| File | |
|---|---|
| `backend-<version>.tar.gz` | app, wheels and OS packages (~140 MB) |
| `backend-<version>.tar.gz.sha256` | checksum |
| `install-backend.sh` | installer / upgrader |
| `README-INSTALL.md` | step-by-step guide for the person installing |

The bundle never contains `.env` files, keys or passwords (it is made with `git archive`,
and the build refuses a dirty tree); each server generates its own at install. Remove old
bundles from `dist/` before copying, so the installer picks the right one.

## 2. Install

Copy the four files into one folder on the offline server (USB disk or internal network)
and run:

```bash
sudo bash install-backend.sh                        # newest backend-*.tar.gz in this folder
```

On a first install it asks for the company **timezone**, the **languages** (English or
Korean; for the web/API, and for the door and till reader screens: Korean on readers only
if their screens can show Korean letters) and the **first admin's email and password**.
Or without questions:

```bash
sudo bash install-backend.sh --timezone Asia/Pyongyang --language ko \
     --device-language en --admin-email admin@chonha.com
```

| Option | Meaning |
|---|---|
| `--timezone Area/City` | Company timezone |
| `--language en\|ko` | Web/API language when the browser doesn't choose one (`ko` = Korean, DPRK usage) |
| `--device-language en\|ko` | Language on door and till reader screens |
| `--hosts "a,b"` | Extra host names / IPs clients use (the server's own are always allowed) |
| `--admin-email EMAIL` | First admin account (asks for the password; or set `DJANGO_SUPERUSER_PASSWORD`) |
| `--no-admin` | Don't create an admin account |
| `--yes` | Don't ask for confirmation |
| `--extract-only [DIR]` | Only check and unpack the bundle (default: next to the script) |

What it does (about 2–3 minutes, without internet):

1. checks the checksum and that the OS matches the bundle;
2. installs the OS packages from the bundle's local apt repository;
3. creates the `backend` service user, `/etc/backend/backend.env` (random secret key and
   database password), the PostgreSQL database and a **self-signed** TLS certificate;
4. installs the application to `/opt/backend/releases/<version>`, applies database
   migrations, collects static files and switches `/opt/backend/current`;
5. **creates all roles and permissions** from the code: ADMIN, BOSS, MANAGER,
   FINANCE_MANAGER, BUILDING_MANAGER, BUILDING_OWNER, SELLER, DEVELOPER;
6. installs and starts the services: `backend-web` (API), `backend-ws` (live updates),
   `backend-tcp` (door devices, TCP 9100), `backend-worker`, nginx;
7. enables backups (WAL archiving, nightly and weekly timers) and runs the first ones;
8. writes timezone, languages and allowed hosts to `/etc/backend/backend.env`, creates the
   first admin (a Django superuser), and runs the health checks.

Success ends with every service `active`, `/health/`, `/health/db/`, `/health/redis/`,
`/health/backup/` returning `200`, and the door listener on 9100 `open`.

By hand, instead of `install-backend.sh`:

```bash
sha256sum -c backend-2026.10.05.tar.gz.sha256      # must say OK
tar -xzf backend-2026.10.05.tar.gz
sudo backend-2026.10.05/app/scripts/install_offline.sh
```

(This skips steps 8: set timezone, languages and hosts in `/etc/backend/backend.env`
and create the admin with `manage.py createsuperuser`.)

## 3. After the first install

Commands that use the application run as the `backend` user with its settings:

```bash
sudo -u backend bash -c 'set -a; . /etc/backend/backend.env; set +a; \
  cd /opt/backend/current && .venv/bin/python manage.py <command>'
```

1. **TLS certificate:** replace `/etc/backend/tls/cert.pem` and `key.pem` with a certificate
   from your internal certificate authority for the server's name or IP, then
   `sudo systemctl reload nginx`. (Otherwise browsers warn about the self-signed one.)
2. **Logins for each role:** `<command>` = `create_role_users --domain chonha.com` creates
   `admin@chonha.com`, `boss@chonha.com`, `manager@chonha.com`, `finance_manager@…`,
   `building_manager@…`, `building_owner@…`, `seller@…`, `developer@…`, each with its
   role. Every new user gets a random password, **printed once**: store them safely.
   `--ask-password` types one password for all; `--roles BOSS BUILDING_OWNER` limits it;
   existing users are skipped.
3. **Link the accounts** that need it: a SELLER login to its store (`user` on the seller),
   BUILDING_OWNER / BUILDING_MANAGER logins to buildings (`owners` / `managers`), a
   DEVELOPER login to a developer profile (`user` on the developer). Leave the SELLER and
   DEVELOPER roles **without permissions**: any permission there applies to all stores.
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
7. **Data:** developers (or import them), sellers, positions and goods. The demo data
   commands (`seed_*`) refuse to run here (they need `DEBUG=true`), by design.

Settings live in `/etc/backend/backend.env` (`TIME_ZONE`, `LANGUAGE_CODE`,
`DEVICE_LANGUAGE`, `DJANGO_ALLOWED_HOSTS`, …). After editing it:
`sudo systemctl restart backend-web backend-ws backend-tcp backend-worker`.

## 4. Upgrades

Build a new bundle, copy the four files over (into the same folder is fine: the newest
bundle is used) and run the same command:

```bash
sudo bash install-backend.sh
```

It **takes a safety backup first** (and stops if that fails), keeps the database and
settings, installs the new release next to the old ones, applies migrations, syncs the
roles and permissions, and restarts the services. Questions aren't asked again; pass an
option (e.g. `--language ko`) to change a setting. Role permissions edited by hand in the
admin are kept: the sync only adds what's new in the code.

**Upgrading from a bundle before 2026.10.05:** BOSS became a read-only role (all data plus
the statistics dashboard) and the new ADMIN role took over full access. So that nobody loses
access, everyone who had BOSS also gets ADMIN: remove ADMIN from those who should only look
at the data (keep it on at least one account). The SELLER_MANAGER role is removed (if
nobody had it).

**Roll back** (only if the new release added no database migrations you need to undo):
```bash
sudo ln -sfn /opt/backend/releases/<previous-version> /opt/backend/current
sudo systemctl restart backend-web backend-ws backend-tcp backend-worker
```

## 5. Not included

- The **frontend** (Next.js) and its Node.js runtime: the frontend team builds it with
  `output: "standalone"` and adds it to the server separately (see `FRONTEND_README.md`).

## Verified

- **2026-10-01:** on a freshly built, minimal Ubuntu 24.04 container with networking
  disabled (no interfaces but loopback), the installer completed in about 2 minutes, all
  services were active and all health checks returned 200. An upgrade from an older bundle
  on the same machine kept existing data and the previous release for rollback.
- **Since then:** the installer's packaging steps are unchanged; new bundles are checked
  for their checksum, contents (no `.env`) and the new options. Database changes are
  tested by the test suite and applied on staging.
