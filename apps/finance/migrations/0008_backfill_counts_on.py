"""Reversals and corrections made before `counts_on` existed: count them on the wrong
deposit's time too (the append-only trigger is off for this one statement)."""

from django.db import migrations

TABLE = "finance_accounttransaction"
TRIGGER = f"{TABLE}_append_only"

BACKFILL = f"""
SET CONSTRAINTS ALL IMMEDIATE;
ALTER TABLE {TABLE} DISABLE TRIGGER {TRIGGER};
-- A correcting deposit: reference "corrects:<id>".
UPDATE {TABLE} AS t
   SET counts_on = COALESCE(w.counts_on, w.created_at)
  FROM {TABLE} AS w
 WHERE t.counts_on IS NULL
   AND t.reference LIKE 'corrects:%%'
   AND w.id::text = substr(t.reference, 10);
-- A reversal: points at the deposit it cancels.
UPDATE {TABLE} AS t
   SET counts_on = COALESCE(w.counts_on, w.created_at)
  FROM {TABLE} AS w
 WHERE t.counts_on IS NULL
   AND t.reverses_id = w.id;
ALTER TABLE {TABLE} ENABLE TRIGGER {TRIGGER};
"""


class Migration(migrations.Migration):
    dependencies = [("finance", "0007_deposit_counts_on")]

    operations = [migrations.RunSQL(BACKFILL, migrations.RunSQL.noop)]
