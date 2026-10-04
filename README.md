# Backend

Django + DRF API. PostgreSQL is required (row locks, partial unique constraints and
append-only triggers); Redis backs the cache and the Celery broker.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements/dev.txt
cp .env.example .env            # then set DJANGO_SECRET_KEY
.venv/bin/python manage.py migrate   # also seeds roles & permissions
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
.venv/bin/celery -A config worker -l info
```

**Development guide for the Windows server** (development copy, ports and how to change
them, testing with devices, demo data, putting changes live):
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

- API: `/api/v1/` · Swagger: `/api/docs/` · Schema: `/api/schema/` · Admin: `/admin/`
- Demo data (dev only): `.venv/bin/python manage.py seed_demo`
- Health: `/health/`, `/health/db/`, `/health/redis/`

## Staging on this server (HTTP :8088 and HTTPS :8443)

nginx ([deploy/nginx/backend-staging.conf](deploy/nginx/backend-staging.conf)) serves the same
app on `http://<ip>:8088` and `https://<ip>:8443` (self-signed certificate in
`/etc/backend/tls/staging-*.pem`). Behind it, `backend-staging`
([deploy/systemd/backend-staging.service](deploy/systemd/backend-staging.service)) runs
gunicorn on `127.0.0.1:8089` with `config.settings.staging` from `.env.staging`, and
`backend-staging-ws` runs uvicorn on `127.0.0.1:8092` for WebSockets (`/ws/`), and
`backend-staging-tcp` listens on **TCP 9100** for the door devices (`$`-framed JSON, see
`docs/DEVICE_INTEGRATION.md`). The backend's own paths (`/api/v1`, `/admin`, `/health`,
`/static`, `/test-console`, ...) go to gunicorn; everything else goes to the frontend,
`frontend-staging` ([deploy/systemd/frontend-staging.service](deploy/systemd/frontend-staging.service)):
the Next.js standalone build in `../frontend` on `127.0.0.1:3100`.
Testing only: the HTTP port sends logins unencrypted.

```bash
systemctl restart backend-staging backend-staging-ws backend-staging-tcp   # after code changes
# after frontend changes (in ../frontend):
npm run build && cp -r .next/static .next/standalone/.next/ && cp -r public .next/standalone/ \
  && systemctl restart frontend-staging
journalctl -u backend-staging -f           # logs
set -a; . ./.env.staging; set +a; .venv/bin/python manage.py collectstatic --noinput  # after static changes
```

## Backups

Continuous WAL archiving (point-in-time recovery to ~1 minute), weekly base backups, and
nightly dumps that are automatically restored and checked; `GET /health/backup/` reports
problems. **Configure an off-site copy** in `/etc/backend/backup.conf`. Full details and
restore runbooks: `docs/BACKUP_AND_RESTORE.md`.

## Test console and sample data (staging)

- **Test console:** `https://<staging-host>:8443/test-console/` (enabled by
  `TEST_CONSOLE_ENABLED=true` in `.env.staging`; never enable it in production). Log in as a
  user with `rfid.device.manage` and `purchase.*` (e.g. the superuser). It lets you:
  - simulate door scans (`in`/`out`, any card or UID, random scans and bursts) and watch the
    live building counts, door feed and who is inside;
  - run the till: pick a counter, build a purchase, tap a card on the purchase's reader,
    enter the PIN and confirm.

  Browsers can't open raw TCP connections, so the page goes through
  `/api/v1/test-console/door-scan/` and `/simulate-tap/`, which skip only the transport
  (the device's IP / TCP connection); everything after that is the real pipeline.
- **Sample data:** `manage.py seed_demo`, then `manage.py seed_more` (DEBUG only; each part
  runs once). This adds 200 more developers with cards and balances, a demo PIN for every
  demo developer (printed once), Demo Bakery, till readers `Reader1`–`Reader4`,
  today's door scans and past purchases. Then
  `manage.py seed_attendance_month [--days 30]` adds a month of realistic door history up
  to yesterday (arrivals, lunch breaks, building changes, absences, forgotten scan-outs) and
  rebuilds attendance from it; days that already have attendance are skipped. Then
  `manage.py seed_purchases_month [--month 2026-09]` adds that month's purchases by
  developers who were present, with ledger, seller and stock entries dated in the month; it
  closes the month with an allowance deposit, seller payouts and a stock delivery so current
  balances and stock don't change, and checks that everything reconciles.

## Deploying to an offline server

Step-by-step guide: `docs/OFFLINE_DEPLOYMENT.md`. Summary:

The production server may have no internet access, so nothing is downloaded at install
time — the app runs natively under systemd behind nginx, not in Docker.

1. **On an online machine with the same OS, CPU architecture and Python version** as the
   server (currently Ubuntu 24.04 / x86_64 / Python 3.12):
   `scripts/build_offline_bundle.sh [version]` → `dist/backend-<version>.tar.gz`
   (app source + every Python dependency as a wheel, ~25 MB).
2. The server needs these OS packages, installed from the Ubuntu install media or
   before it goes offline: `python3-venv postgresql redis-server nginx openssl`.
3. Copy the bundle over (USB, internal network) and run as root:
   ```bash
   tar -xzf backend-<version>.tar.gz && cd backend-<version>
   sudo app/scripts/install_offline.sh
   ```
   First install creates the `backend` user, `/etc/backend/backend.env` (random secret
   key and DB password), the Postgres role/DB and a self-signed TLS certificate
   (replace `/etc/backend/tls/*.pem` with your internal CA's certificate). Every run
   installs the release to `/opt/backend/releases/<version>`, migrates, collects static
   files, switches `/opt/backend/current` and restarts `backend-web` (REST API, gunicorn),
   `backend-ws` (WebSockets, uvicorn), `backend-tcp` (door listener, TCP 9100) and
   `backend-worker` (Celery). The doors must be able to reach TCP 9100 on the server.
4. Roll back: `ln -sfn /opt/backend/releases/<old> /opt/backend/current && systemctl
   restart backend-web backend-worker` (only safe if the newer release added no
   irreversible migrations).

`requirements/constraints.txt` pins the exact versions the bundle installs; regenerate it
after upgrading packages in the dev venv.

## Tests & lint

```bash
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## Conventions

- **Errors** always look like `{"error": {"code", "message", "details?"}}`. Business
  rules raise a `common.exceptions.DomainError` subclass with a stable `code`.
- **Permissions**: views use `common.permissions.HasPermissions` with a
  `required_permissions = {action: [codenames]}` map. Unmapped actions are denied.
  New permissions go in `apps/accounts/rbac.py`; `migrate` syncs them.
- **Business logic** lives in `services.py`, not in views or serializers. A service owns
  its `transaction.atomic()` and calls `apps.audit.services.record_audit` inside it.
- **Immutable tables** (audit log, ledgers) inherit `common.models.AppendOnlyModel` and
  add `common.db.append_only_trigger(table)` to their migration.
- **Soft delete**: inherit `common.models.SoftDeleteModel`; make unique constraints
  partial on `deleted_at IS NULL`.

## Translations (English / Korean, DPRK usage)

User-facing messages are wrapped in `gettext_lazy` (`_("...")`); clients pick the language
with `Accept-Language`. After adding or changing a message:

```bash
.venv/bin/python manage.py makemessages -l ko_KP --no-location --no-wrap \
    -i ".venv/*" -i "tests/*" -i "*/migrations/*" -i "*/management/*" -i "scripts/*"
# translate the new entries in locale/ko_KP/LC_MESSAGES/django.po, then:
.venv/bin/python manage.py compilemessages -l ko_KP
```

Commit both `django.po` and `django.mo` (the offline server has no gettext tools).
`tests/test_i18n.py` fails if a message is untranslated or the `.mo` is stale.

## Logins for each role

```bash
.venv/bin/python manage.py create_role_users   # admin, boss, finance_manager, ... (--prefix chonha_ for chonha_admin, ...)
```

Creates one user per role (skips existing ones) with random passwords printed once;
`--ask-password` to type one, `--roles ...` for only some.
