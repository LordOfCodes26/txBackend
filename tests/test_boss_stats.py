"""BOSS: sees all data and the company statistics, changes nothing. ADMIN: full access."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Permission, Role, UserRole
from apps.accounts.rbac import ALL, VIEW, Roles
from apps.attendance.models import DailyAttendance
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import TransactionKind
from apps.rfid.models import Building
from apps.sellers.models import Seller

pytestmark = pytest.mark.django_db

STATS = "/api/v1/stats/"


@pytest.fixture
def boss_client(auth_client, make_user):
    return auth_client(make_user(Roles.BOSS, email="ceo@x.com"))


def test_roles_in_the_catalog():
    assert (
        set(Role.objects.get(code=Roles.ADMIN).permissions.values_list("codename", flat=True))
        == ALL
    )
    boss = set(Role.objects.get(code=Roles.BOSS).permissions.values_list("codename", flat=True))
    assert boss == VIEW
    assert "stats.view" in boss and not any(p.endswith((".create", ".manage")) for p in boss)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/users/",
        "/api/v1/developers/",
        "/api/v1/rfid/cards/",
        "/api/v1/attendance/records/",
        "/api/v1/attendance/occupancy/",
        "/api/v1/finance/accounts/",
        "/api/v1/sellers/",
        "/api/v1/goods/",
        "/api/v1/purchases/",
        "/api/v1/purchases/performance/",
        "/api/v1/bookings/",
        "/api/v1/audit-logs/",
        STATS,
    ],
)
def test_boss_reads_everything(boss_client, path):
    assert boss_client.get(path).status_code == 200, path


def test_boss_changes_nothing(boss_client, make_user):
    dev = Developer.objects.create(employee_number="E1", full_name="Ada")
    seller = Seller.objects.create(name="Cafe")
    assert (
        boss_client.post(
            "/api/v1/developers/", {"employee_number": "E2", "full_name": "B"}
        ).status_code
        == 403
    )
    assert boss_client.patch(f"/api/v1/developers/{dev.pk}/", {"phone": "1"}).status_code == 403
    assert boss_client.patch(f"/api/v1/sellers/{seller.pk}/", {"name": "X"}).status_code == 403
    assert boss_client.post("/api/v1/sellers/", {"name": "New"}).status_code == 403
    other = make_user()
    r = boss_client.post(f"/api/v1/users/{other.pk}/roles/", {"role": Roles.DEVELOPER})
    assert r.status_code == 403


def test_stats_need_stats_view(auth_client, make_user):
    assert auth_client(make_user(Roles.MANAGER)).get(STATS).status_code == 403
    assert auth_client(make_user(Roles.ADMIN)).get(STATS).status_code == 200


def test_period_validation(boss_client):
    r = boss_client.get(f"{STATS}?date_from=2026-10-05&date_to=2026-10-01")
    assert "date_from" in r.json()["error"]["details"]
    r = boss_client.get(f"{STATS}?date_from=2024-01-01&date_to=2026-10-01")
    assert "date_from" in r.json()["error"]["details"]
    body = boss_client.get(STATS).json()
    assert body["period"]["days"] == 30
    assert body["period"]["date_to"] == timezone.localdate().isoformat()


def test_people_and_attendance(boss_client):
    b1 = Building.objects.create(code="B1", name="Building 1")
    ada = Developer.objects.create(employee_number="E1", full_name="Ada", building=b1)
    bob = Developer.objects.create(employee_number="E2", full_name="Bob", status="ON_LEAVE")
    Developer.objects.create(employee_number="E3", full_name="Cy", status="TERMINATED")
    today = timezone.localdate()
    yesterday = today - timedelta(days=1)
    now = timezone.now()
    for dev, day, hours in [(ada, yesterday, 8), (bob, yesterday, 6), (ada, today, 4)]:
        DailyAttendance.objects.create(
            developer=dev,
            work_date=day,
            first_seen=now,
            last_seen=now,
            record_count=2,
            worked_seconds=hours * 3600,
        )

    body = boss_client.get(f"{STATS}?date_from={yesterday}&date_to={today}").json()
    devs = body["people"]["developers"]
    assert devs["total"] == 2  # terminated not counted
    assert devs["by_status"] == {"ACTIVE": 1, "ON_LEAVE": 1, "SUSPENDED": 0, "TERMINATED": 1}
    assert {(b["name"], b["count"]) for b in devs["by_building"]} == {("Building 1", 1), (None, 1)}
    attendance = body["people"]["attendance"]
    assert attendance["daily"] == [
        {"date": yesterday.isoformat(), "present": 2, "avg_worked_hours": 7.0},
        {"date": today.isoformat(), "present": 1, "avg_worked_hours": 4.0},
    ]
    assert (attendance["avg_present_per_day"], attendance["avg_worked_hours"]) == (1.5, 5.5)
    assert "total" in body["people"]["inside_now"]


def test_money(boss_client, make_user):
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    account = finance.open_account(ada)
    finance.deposit(actor=None, developer=ada, amount="100.00", idempotency_key="stats-0001")
    with transaction.atomic():
        finance.post_transaction(
            account=account,
            kind=TransactionKind.PURCHASE,
            amount=Decimal("-30.00"),
            actor=None,
            description="test",
            reference="t",
        )

    body = boss_client.get(STATS).json()["money"]
    assert body["developer_accounts"]["total_balance"] == "70.00"
    assert body["deposits"] == {"total": "100.00", "count": 1}
    assert body["spending"] == {"total": "30.00", "count": 1}
    today = [d for d in body["daily"] if d["date"] == timezone.localdate().isoformat()]
    assert today == [
        {"date": timezone.localdate().isoformat(), "deposits": "100.00", "spending": "30.00"}
    ]
    assert "sellers" not in body


def test_migration_keeps_existing_bosses_working(make_user):
    """Existing BOSS users also get ADMIN; the BOSS role becomes read-only."""
    import importlib

    migration = importlib.import_module("apps.accounts.migrations.0003_admin_role_boss_read_only")
    old_boss = make_user(Roles.BOSS)
    Role.objects.get(code=Roles.BOSS).permissions.set(Permission.objects.all())  # old setup

    migration.split_admin_from_boss(django_apps, None)

    assert UserRole.objects.filter(user=old_boss, role__code=Roles.ADMIN).exists()
    boss = set(Role.objects.get(code=Roles.BOSS).permissions.values_list("codename", flat=True))
    assert boss == VIEW
