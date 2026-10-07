#!/usr/bin/env bash
# Restore a dump into a scratch database and check it is complete and consistent:
# restore succeeds, key tables have rows, ledgers and stock reconcile. Drops the scratch DB.
DIR=$(cd "$(dirname "$0")" && pwd)
. "$DIR/common.sh"
dump=${1:?usage: verify.sh <dumpfile>}
scratch="${DB_NAME}_verify"

sha256sum --check --quiet "$dump.sha256"
as_postgres dropdb --if-exists "$scratch"
as_postgres createdb "$scratch"
trap 'as_postgres dropdb --if-exists "$scratch"' EXIT
as_postgres pg_restore --no-owner --exit-on-error --dbname="$scratch" "$dump"

result=$(as_postgres psql -d "$scratch" -tA -v ON_ERROR_STOP=1 <<'SQL'
SELECT json_build_object(
  'developers',          (SELECT count(*) FROM developers_developer),
  'transactions',        (SELECT count(*) FROM finance_accounttransaction),
  'attendance_records',  (SELECT count(*) FROM attendance_attendancerecord),
  'migrations',          (SELECT count(*) FROM django_migrations),
  'developer_ledger_mismatches', (
     SELECT count(*) FROM finance_developeraccount a
     WHERE a.balance <> COALESCE((SELECT sum(amount) FROM finance_accounttransaction t
                                  WHERE t.account_id = a.id), 0)),
  'stock_mismatches', (
     SELECT count(*) FROM goods_good g
     WHERE g.track_stock AND g.quantity <> COALESCE((SELECT sum(quantity_delta)
            FROM goods_inventorymovement m WHERE m.good_id = g.id), 0))
);
SQL
)
echo "verify: $result"
python3 - "$result" <<'PY'
import json, sys
r = json.loads(sys.argv[1])
problems = [k for k in ("developer_ledger_mismatches", "stock_mismatches") if r[k]]
if r["migrations"] == 0:
    problems.append("no migrations table rows")
if problems:
    sys.exit("verify failed: " + ", ".join(problems))
PY
update_status "last_verify_counts=$result"
