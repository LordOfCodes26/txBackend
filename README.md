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

- API: `/api/v1/` · Swagger: `/api/docs/` · Schema: `/api/schema/` · Admin: `/admin/`
- Demo data (dev only): `.venv/bin/python manage.py seed_demo`
- Health: `/health/`, `/health/db/`, `/health/redis/`

## Staging on this server (HTTP :8088 and HTTPS :8443)

nginx ([deploy/nginx/backend-staging.conf](deploy/nginx/backend-staging.conf)) serves the same
app on `http://<ip>:8088` and `https://<ip>:8443` (self-signed certificate in
`/etc/backend/tls/staging-*.pem`). Behind it, `backend-staging`
([deploy/systemd/backend-staging.service](deploy/systemd/backend-staging.service)) runs
gunicorn on `127.0.0.1:8089` with `config.settings.staging` from `.env.staging`, and
`backend-staging-ws` runs uvicorn on `127.0.0.1:8092` for WebSockets (`/ws/`).
Testing only: the HTTP port sends logins unencrypted.

```bash
systemctl restart backend-staging backend-staging-ws   # after code changes
journalctl -u backend-staging -f           # logs
set -a; . ./.env.staging; set +a; .venv/bin/python manage.py collectstatic --noinput  # after static changes
```

## Deploying to an offline server

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
   `backend-ws` (WebSockets, uvicorn) and `backend-worker` (Celery).
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
