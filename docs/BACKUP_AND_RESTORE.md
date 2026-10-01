# Backup and Restore

Everything that matters (developers, balances and ledgers, purchases, attendance, audit
log) lives in one PostgreSQL database. Uploaded images live in `MEDIA_ROOT`. Redis holds
only temporary data (cache, live events) and needs no backup.

## What runs automatically

| What | When | Gives you | Data you could lose |
|---|---|---|---|
| **WAL archiving**: every database change is copied to `/var/backups/backend/wal` | Continuously (at least once a minute while active) | Restore to **any point in time** | ~1 minute |
| **Base backup** (`pg_basebackup`) | Sundays 03:30 (`backend-basebackup.timer`), and once at install | The starting point that WAL is replayed onto | none |
| **Nightly dump** (`pg_dump`) + media archive | Daily 02:30 (`backend-backup.timer`) | A simple, portable full copy | up to 24 h (on its own) |
| **Restore check** | After every nightly dump | Proof the dump restores: tables present, ledgers and stock reconcile | none |
| **Off-site copy** (`rsync` of `/var/backups/backend`) | After each backup | Survives losing this server's disk | since the last copy |

Retention (in `/etc/backend/backup.conf`): 14 days of nightly dumps, the newest 4 base
backups, and the WAL needed to replay from the oldest kept base backup (about 4 weeks of
point-in-time recovery).

## Off-site copy: set this up

Backups on the same disk die with it. In `/etc/backend/backup.conf` set **one** of:

- `OFFSITE_DIR=/mnt/backup-disk/backend`: a mounted second disk, USB disk or NAS share, or
- `OFFSITE_RSYNC=backup@10.0.0.50:/srv/backups/backend`: another server over SSH (set up
  key-based login for root first).

Ideally use a machine in **another room or building**. The backups contain personal data
and PIN hashes: keep the target access-restricted (and encrypted at rest, e.g. an encrypted
disk).

## Monitoring

`GET /health/backup/` returns **503** when the nightly dump is older than 26 h, the last
restore check failed, the base backup is older than 8 days, or WAL archiving is off or
failing. It returns 200 with `warnings` (e.g. no off-site copy configured) otherwise. Add it
to whatever monitoring you have, or check it daily. Logs: `journalctl -u backend-backup`,
`journalctl -u backend-basebackup`.

## Restore runbooks

Practise these on a test machine **before** you need them, and repeat every few months.

### A. Restore the latest nightly dump (simplest)

Use when: the database is corrupt or lost, and losing the changes since last night is
acceptable (or you'll use runbook B instead).

```bash
ls -1t /var/backups/backend/db/*.dump | head          # pick a dump
sudo /opt/backend/current/scripts/backup/restore_dump.sh /var/backups/backend/db/backend-<stamp>.dump --yes
```

The script checks the dump's checksum and restores it into a scratch database first. Only
then does it stop the app, **rename the current database** to `backend_before_restore_<time>`
(it is never deleted automatically), restore the dump as the live database and start the
app again. Afterwards: `manage.py check_ledger` and `manage.py check_inventory`, then drop the
kept database when you're satisfied.

### B. Point-in-time recovery (to the minute before a mistake)

Use when: data was deleted or damaged at a known time (e.g. "a wrong import at 10:42"), or
the server died and you want everything up to the last archived minute.

```bash
TARGET="2026-10-01 10:41:00+00"         # just before the mistake, with timezone
PGVER=16                                 # PostgreSQL major version
DATA=/var/lib/postgresql/$PGVER/main
BASE=$(ls -1d /var/backups/backend/base/*/ | sort | tail -1)   # newest base backup BEFORE $TARGET

sudo systemctl stop backend-web backend-ws backend-tcp backend-worker
sudo systemctl stop postgresql
sudo mv "$DATA" "$DATA.before_pitr_$(date +%Y%m%d%H%M)"       # keep the damaged data directory
sudo install -d -o postgres -g postgres -m 0700 "$DATA"
sudo -u postgres tar -xzf "$BASE/base.tar.gz" -C "$DATA"
sudo -u postgres tee -a "$DATA/postgresql.auto.conf" <<CONF
restore_command = 'cp /var/backups/backend/wal/%f %p'
recovery_target_time = '$TARGET'
recovery_target_action = 'promote'
CONF
sudo -u postgres touch "$DATA/recovery.signal"
sudo systemctl start postgresql
sudo tail -f /var/log/postgresql/postgresql-$PGVER-main.log   # wait for "archive recovery complete"
```

Then remove the three recovery lines from `$DATA/postgresql.auto.conf`, run
`manage.py check_ledger`, `manage.py check_inventory` and `manage.py rebuild_attendance`, and
start the app services. Finally take a fresh base backup at once
(`sudo systemctl start backend-basebackup`), since recovery starts a new timeline.

To recover **everything up to the last archived change** (e.g. after losing the data disk),
leave out `recovery_target_time`.

If the backup files live only off-site, copy `/var/backups/backend` back from the off-site
target first.

### C. Restore uploaded images

```bash
sudo tar -xzf /var/backups/backend/media/media-<stamp>.tar.gz -C /var/lib/backend/media
```

## Verified on staging (2026-10-01)

- A nightly dump restored into a scratch database: 230 developers, 253 transactions and 546
  attendance records, with zero ledger or stock mismatches.
- Point-in-time recovery into a scratch PostgreSQL: a row written 2 seconds **before** the
  target time was present, and a row written 2 seconds **after** it was absent.
