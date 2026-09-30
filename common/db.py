from django.db import migrations

_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION forbid_append_only_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Table % is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
"""


def append_only_trigger(table: str) -> migrations.RunSQL:
    """Migration operation that makes `table` reject UPDATE and DELETE at the DB level."""
    trigger = f"{table}_append_only"
    return migrations.RunSQL(
        sql=_FUNCTION_SQL
        + f"""
        CREATE TRIGGER {trigger}
        BEFORE UPDATE OR DELETE ON {table}
        FOR EACH ROW EXECUTE FUNCTION forbid_append_only_mutation();
        """,
        reverse_sql=f"DROP TRIGGER IF EXISTS {trigger} ON {table};",
    )
