"""New developer with their card (tapped on a card assign reader) and PIN, in one step."""

import pytest
from django.contrib.auth.hashers import check_password

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.finance.models import DeveloperAccount
from apps.rfid.models import RFIDCard, RFIDCardAssignment

URL = "/api/v1/developers/"


@pytest.fixture
def admin(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN, username="root"))


@pytest.fixture
def card(db):
    return RFIDCard.objects.create(uid="04CAFE01", label="0101")


def new(client, **extra):
    return client.post(URL, {"employee_number": "E1", "full_name": "Ada", **extra}, format="json")


def test_developer_with_card_and_pin(admin, card):
    r = new(admin, card=card.pk, pin="4826", pin_confirm="4826")
    assert r.status_code == 201, r.json()
    dev = Developer.objects.get(employee_number="E1")
    assert RFIDCardAssignment.objects.get(unassigned_at__isnull=True).developer == dev
    assert check_password("4826", DeveloperAccount.objects.get(developer=dev).pin_hash)


def test_without_a_card_as_before(admin):
    assert new(admin).status_code == 201
    assert not RFIDCardAssignment.objects.exists()


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"pin": "4826", "pin_confirm": "4827"}, "pin_confirm"),
        ({"pin": "1234", "pin_confirm": "1234"}, "pin"),
        ({"pin": "", "pin_confirm": ""}, "pin"),
    ],
)
def test_pin_problems_save_nothing(admin, card, extra, field):
    r = new(admin, card=card.pk, **extra)
    assert r.status_code == 400 and field in r.json()["error"]["details"], r.json()
    assert not Developer.objects.exists()


def test_card_already_taken_and_developer_problems_together(admin, card):
    other = Developer.objects.create(employee_number="E9", full_name="Bob")
    RFIDCardAssignment.objects.create(card=card, developer=other)
    r = admin.post(
        URL,
        {
            "employee_number": "E9",
            "full_name": "Ada",
            "card": card.pk,
            "pin": "4826",
            "pin_confirm": "4826",
        },
        format="json",
    )
    details = r.json()["error"]["details"]
    assert {"card", "employee_number"} <= set(details), details
    assert Developer.objects.count() == 1


def test_card_needs_card_assign_rights(auth_client, make_user, card):
    from apps.accounts.models import Role

    user = make_user(Roles.MANAGER)
    Role.objects.get(code=Roles.MANAGER).permissions.filter(codename="rfid.assign").delete()
    client = auth_client(user)
    r = new(client, card=card.pk, pin="4826", pin_confirm="4826")
    assert r.status_code == 403 and not Developer.objects.exists()
