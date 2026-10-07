"""Excel import (developers, cards, opening balances) and export (developers, money, goods)."""

import io
from datetime import date
from decimal import Decimal

import pytest
from django.db import transaction
from openpyxl import Workbook, load_workbook

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.finance.models import AccountTransaction, DeveloperAccount
from apps.goods import services as goods
from apps.goods.models import Good
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment
from apps.sellers.models import Seller, ServicePosition

pytestmark = pytest.mark.django_db

IMPORT = "/api/v1/imports/{}/"
EXPORT = "/api/v1/exports/{}/"


def xlsx(*rows) -> io.BytesIO:
    book = Workbook()
    for row in rows:
        book.active.append(list(row))
    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    buffer.name = "upload.xlsx"
    return buffer


def upload(client, kind, *rows, dry_run=False):
    return client.post(
        IMPORT.format(kind), {"file": xlsx(*rows), "dry_run": dry_run}, format="multipart"
    )


def read(response) -> dict[str, list[tuple]]:
    assert response.status_code == 200, response.content[:300]
    assert response["Content-Type"].startswith("application/vnd.openxmlformats")
    book = load_workbook(io.BytesIO(response.content))
    return {ws.title: list(ws.iter_rows(values_only=True)) for ws in book.worksheets}


@pytest.fixture
def b1(db):
    return Building.objects.create(code="B1", name="Building 1")


@pytest.fixture
def admin(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN, username="root"))


# --- Developers ---------------------------------------------------------------------


def test_import_creates_and_updates_developers(admin, b1):
    Developer.objects.create(employee_number="E1", full_name="Old Name", department="QA")
    r = upload(
        admin,
        "developers",
        ("Employee no.", "Full name", "Department", "Building", "Start date", "Status"),
        ("E1", "Ada Lovelace", None, "b1", None, None),  # blank cells: unchanged
        (1002, "Alan Turing", "Research", "Building 1", date(2026, 10, 1), "on leave"),
    )
    body = r.json()
    assert (body["created"], body["updated"], body["errors"], body["saved"]) == (1, 1, [], True)
    ada = Developer.objects.get(employee_number="E1")
    assert (ada.full_name, ada.department, ada.building) == ("Ada Lovelace", "QA", b1)
    alan = Developer.objects.get(employee_number="1002")  # a number cell becomes text
    assert (alan.start_date, alan.status, alan.building) == (date(2026, 10, 1), "ON_LEAVE", b1)
    assert DeveloperAccount.objects.filter(developer=alan).exists()  # same service as the form
    assert AuditLog.objects.filter(action="developer.created").count() == 1


def test_any_error_saves_nothing_and_lists_every_row(admin, b1):
    r = upload(
        admin,
        "developers",
        ("employee_number", "full_name", "building", "status", "birthday"),
        ("E1", "Ada", "B1", "ACTIVE", None),
        ("E2", "Bob", "B9", "ACTIVE", None),
        ("E3", "Cy", "B1", "RETIRED", None),
        ("E4", "Di", "B1", "ACTIVE", "2999-01-01"),
        ("E1", "Ada again", "B1", "ACTIVE", None),
    )
    body = r.json()
    assert body["saved"] is False and not Developer.objects.exists()
    assert [(e["row"], e["column"]) for e in body["errors"]] == [
        (3, "Building"),
        (4, "Status"),
        (5, "Birthday"),
        (6, "Employee no."),
    ]


def test_dry_run_checks_without_saving(admin, b1):
    body = upload(
        admin, "developers", ("Employee no.", "Full name"), ("E1", "Ada"), dry_run=True
    ).json()
    assert (body["created"], body["errors"], body["dry_run"], body["saved"]) == (1, [], True, False)
    assert not Developer.objects.exists() and not AuditLog.objects.exists()


def test_missing_column_or_bad_file(admin):
    r = upload(admin, "developers", ("Full name",), ("Ada",))
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_WORKBOOK"
    bad = io.BytesIO(b"not a workbook")
    bad.name = "x.xlsx"
    r = admin.post(IMPORT.format("developers"), {"file": bad}, format="multipart")
    assert r.json()["error"]["code"] == "BAD_WORKBOOK"


def test_korean_headers(admin):
    body = upload(admin, "developers", ("직원번호", "이름"), ("E7", "김철")).json()
    # Headers match the labels in either language (Korean from the .po file).
    assert body["saved"] is True, body


def test_building_manager_imports_only_into_own_buildings(auth_client, make_user, b1):
    b2 = Building.objects.create(code="B2", name="Building 2")
    manager = make_user(Roles.BUILDING_MANAGER)
    b1.managers.add(manager)
    Developer.objects.create(employee_number="X1", full_name="Other", building=b2)
    client = auth_client(manager)
    body = upload(
        client,
        "developers",
        ("Employee no.", "Full name", "Building"),
        ("E1", "Ada", "B1"),
        ("E2", "Bob", "B2"),
        ("X1", "Renamed", None),
    ).json()
    assert [(e["row"], e["column"]) for e in body["errors"]] == [
        (3, "Building"),
        (4, "Employee no."),
    ]


def test_export_round_trips_into_import(admin, b1):
    dev = Developer.objects.create(
        employee_number="E1", full_name="Ada", building=b1, birthday=date(1990, 5, 1)
    )
    card = RFIDCard.objects.create(uid="04AABBCC", label="0001")
    RFIDCardAssignment.objects.create(card=card, developer=dev)
    sheets = read(admin.get(EXPORT.format("developers")))
    rows = sheets["Developers"]
    assert rows[0][:2] == ("Employee no.", "Full name") and rows[0][-2:] == (
        "Card UID",
        "Card label",
    )
    assert rows[1][0] == "E1" and rows[1][7] == "B1" and rows[1][-2:] == ("04AABBCC", "0001")
    # The same file imports back without changes (developers) and as cards.
    body = upload(admin, "developers", *rows).json()
    assert (body["unchanged"], body["errors"]) == (1, [])
    body = upload(admin, "cards", *rows).json()
    assert (body["unchanged"], body["errors"]) == (1, []), body


# --- Cards --------------------------------------------------------------------------


def test_import_registers_labels_and_assigns_cards(admin):
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    Developer.objects.create(employee_number="E2", full_name="Bob")
    RFIDCard.objects.create(uid="04000001", label="old")
    body = upload(
        admin,
        "cards",
        ("Card UID", "Card label", "Employee no."),
        ("04:00:00:01", "0001", "E1"),  # existing card, new label, assign
        ("04000002", "0002", None),  # new, unassigned
    ).json()
    assert (body["created"], body["updated"], body["errors"]) == (1, 1, [])
    assert RFIDCard.objects.get(uid="04000001").label == "0001"
    assert RFIDCardAssignment.objects.get(unassigned_at__isnull=True).developer == ada
    assert RFIDCard.objects.filter(uid="04000002").exists()


def test_card_errors(admin):
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="0400AA"), developer=ada)
    body = upload(
        admin,
        "cards",
        ("Card UID", "Employee no."),
        ("XYZ", None),
        ("0400BB", "E9"),
        ("0400CC", "E1"),  # Ada already has a card
    ).json()
    assert [e["row"] for e in body["errors"]] == [2, 3, 4]
    assert body["errors"][0]["column"] == "Card UID"
    assert RFIDCard.objects.count() == 1


# --- Opening balances ---------------------------------------------------------------


def test_opening_balances_are_deposits_once(admin):
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    Developer.objects.create(employee_number="E2", full_name="Bob")
    rows = (("Employee no.", "Amount", "Description"), ("E1", 120.5, None), ("E2", "80", "Old"))
    body = upload(admin, "balances", *rows).json()
    assert (body["created"], body["errors"]) == (2, [])
    assert DeveloperAccount.objects.get(developer=ada).balance == Decimal("120.50")
    txn = AccountTransaction.objects.get(account__developer=ada)
    assert (txn.kind, txn.description) == ("DEPOSIT", "Opening balance")
    # The same balances again (a re-saved file, other order) add nothing.
    body = upload(admin, "balances", *rows).json()
    assert (body["created"], body["unchanged"]) == (0, 2)
    assert DeveloperAccount.objects.get(developer=ada).balance == Decimal("120.50")


def test_balance_errors(admin, settings):
    settings.FINANCE_MAX_DEPOSIT = "1000.00"
    Developer.objects.create(employee_number="E1", full_name="Ada")
    body = upload(
        admin,
        "balances",
        ("Employee no.", "Amount"),
        ("E1", "abc"),
        ("E1", -5),
        ("E9", 10),
    ).json()
    assert [(e["row"], e["column"]) for e in body["errors"]] == [
        (2, "Amount"),
        (3, "Employee no."),  # E1 twice
        (4, "Employee no."),
    ]
    body = upload(admin, "balances", ("Employee no.", "Amount"), ("E1", 5000)).json()
    assert body["errors"][0]["row"] == 2 and not AccountTransaction.objects.exists()


# --- Permissions and templates ------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "kind", "allowed"),
    [
        (Roles.BOSS, "developers", False),
        (Roles.FINANCE_MANAGER, "developers", False),
        (Roles.FINANCE_MANAGER, "balances", True),
        (Roles.MANAGER, "cards", True),
        (Roles.MANAGER, "balances", False),
    ],
)
def test_import_permissions(auth_client, make_user, role, kind, allowed):
    client = auth_client(make_user(role))
    r = client.get(IMPORT.format(kind) + "template/")
    assert (r.status_code == 200) == allowed
    if allowed:
        rows = [row for row in next(iter(read(r).values())) if any(row)]
        assert len(rows) == 2  # header + example row
        sheet = load_workbook(io.BytesIO(r.content)).worksheets[0]
        assert sheet["A3"].number_format == "@"  # text: keeps leading zeros


@pytest.mark.parametrize(
    ("role", "kind", "allowed"),
    [
        (Roles.BOSS, "developers", True),
        (Roles.BOSS, "money", True),
        (Roles.SELLER, "goods", False),
        (Roles.MANAGER, "money", False),
        (Roles.FINANCE_MANAGER, "money", True),
    ],
)
def test_export_permissions(auth_client, make_user, role, kind, allowed):
    r = auth_client(make_user(role)).get(EXPORT.format(kind))
    assert (r.status_code == 200) == allowed, r.status_code


def test_unknown_kind(admin):
    assert admin.get(EXPORT.format("nope")).status_code == 404
    assert admin.get(IMPORT.format("nope") + "template/").status_code == 404


# --- Money and goods exports --------------------------------------------------------


def test_money_and_goods_exports(admin, make_user, b1, auth_client):
    ada = Developer.objects.create(employee_number="E1", full_name="Ada", building=b1)
    finance.deposit(actor=None, developer=ada, amount="50.00", idempotency_key="x-1")
    seller = Seller.objects.create(name="Cafe")
    position = ServicePosition.objects.create(seller=seller, name="Counter", building=b1)
    tea = Good.objects.create(service_position=position, name="Tea", price="2.50")
    with transaction.atomic():
        goods.move_stock(good=tea, kind="INITIAL_STOCK", delta=10)

    money = read(admin.get(EXPORT.format("money")))
    assert list(money) == ["Balances", "Transactions", "Purchases"]
    assert money["Balances"][1][0] == "E1" and money["Balances"][1][-1] == 50
    assert money["Transactions"][1][3:5] == ("DEPOSIT", 50)
    stock = read(admin.get(EXPORT.format("goods")))
    assert stock["Goods"][1][:3] == ("Cafe", "Counter", "Tea") and stock["Goods"][1][6] == 10
    assert stock["Stock movements"][1][3:5] == ("INITIAL_STOCK", 10)

    r = admin.get(EXPORT.format("money") + "?date_from=2026-10-05&date_to=2026-10-01")
    assert r.status_code == 400
