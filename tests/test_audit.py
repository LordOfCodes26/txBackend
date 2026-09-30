import pytest
from django.db import IntegrityError, connection, transaction

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.audit.services import record_audit

pytestmark = pytest.mark.django_db


def test_audit_log_rejects_update_and_delete_in_python(boss):
    log = record_audit("test.action", actor=boss, entity=boss)
    log.action = "tampered"
    with pytest.raises(TypeError):
        log.save()
    with pytest.raises(TypeError):
        log.delete()


def test_audit_log_is_append_only_at_database_level(boss):
    record_audit("test.action", actor=boss, entity=boss)
    for statement in ("UPDATE audit_auditlog SET action = 'x'", "DELETE FROM audit_auditlog"):
        with pytest.raises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(statement)
    assert AuditLog.objects.get().action == "test.action"
    with pytest.raises(IntegrityError), transaction.atomic():
        AuditLog.objects.all().update(action="bulk")


def test_record_audit_requires_an_entity():
    with pytest.raises(ValueError):
        record_audit("test.action")


def test_system_actions_have_no_actor():
    log = record_audit("system.task", entity_type="rfid.device", entity_id="READER-1")
    assert log.actor is None and log.actor_email == ""


@pytest.mark.parametrize(("role", "expected"), [(Roles.BOSS, 200), (Roles.DEVELOPER, 403)])
def test_audit_log_api_access(auth_client, make_user, role, expected):
    assert auth_client(make_user(role)).get("/api/v1/audit-logs/").status_code == expected


def test_audit_log_api_filters(auth_client, boss):
    record_audit("a.one", actor=boss, entity=boss)
    record_audit("a.two", actor=boss, entity=boss)
    results = auth_client(boss).get("/api/v1/audit-logs/?action=a.two").json()["results"]
    assert [r["action"] for r in results] == ["a.two"]
