"""The developer list shows each developer's current card label; it is searchable and
sortable."""

import pytest

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard

URL = "/api/v1/developers/"


@pytest.fixture
def people(db):
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    bob = Developer.objects.create(employee_number="E2", full_name="Bob")
    Developer.objects.create(employee_number="E3", full_name="Cy")  # no card
    old = RFIDCard.objects.create(uid="04AA", label="0099")
    rfid.assign_card(actor=None, card=old, developer=ada)
    rfid.unassign_card(actor=None, card=old)  # returned: no longer Ada's
    rfid.assign_card(
        actor=None, card=RFIDCard.objects.create(uid="04BB", label="0123"), developer=ada
    )
    rfid.assign_card(
        actor=None, card=RFIDCard.objects.create(uid="04CC", label="0456"), developer=bob
    )
    return ada, bob


def test_card_label_search_and_sort(auth_client, make_user, people):
    client = auth_client(make_user(Roles.MANAGER))
    rows = client.get(URL).json()["results"]
    assert {r["full_name"]: r["card_label"] for r in rows} == {
        "Ada": "0123",
        "Bob": "0456",
        "Cy": None,
    }
    # The card number finds its holder; a returned card doesn't.
    assert [r["full_name"] for r in client.get(f"{URL}?search=0456").json()["results"]] == ["Bob"]
    assert client.get(f"{URL}?search=0099").json()["count"] == 0
    ordered = client.get(f"{URL}?ordering=-card_label").json()["results"]
    assert [r["full_name"] for r in ordered][:2] == ["Bob", "Ada"]
    ada, _bob = people
    assert client.get(f"{URL}{ada.pk}/").json()["card_label"] == "0123"
