"""A deleted developer can be brought back by those who may delete developers; their card
stays unassigned (it was taken back when they were deleted)."""

import pytest

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer
from apps.rfid.models import RFIDCard, RFIDCardAssignment

DEVELOPERS = "/api/v1/developers/"


@pytest.mark.django_db
def test_delete_then_restore(auth_client, make_user):
    admin = auth_client(make_user(Roles.ADMIN))
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04AA"), developer=ada)
    assert admin.delete(f"{DEVELOPERS}{ada.pk}/").status_code == 204
    assert admin.get(f"{DEVELOPERS}{ada.pk}/").status_code == 404
    deleted = admin.get(f"{DEVELOPERS}deleted/").json()["results"]
    assert [(d["full_name"], d["deleted_at"] is not None) for d in deleted] == [("Ada", True)]

    r = admin.post(f"{DEVELOPERS}{ada.pk}/restore/")
    assert r.status_code == 200, r.json()
    assert r.json()["deleted_at"] is None
    assert admin.get(f"{DEVELOPERS}{ada.pk}/").status_code == 200
    assert admin.get(f"{DEVELOPERS}deleted/").json()["results"] == []
    assert AuditLog.objects.filter(action="developer.restored").count() == 1
    # The card was taken back on delete and is not given back by itself.
    assert not RFIDCardAssignment.objects.filter(developer=ada, unassigned_at__isnull=True).exists()


@pytest.mark.django_db
def test_restore_refused_when_the_number_is_taken(auth_client, make_user):
    admin = auth_client(make_user(Roles.ADMIN))
    old = Developer.objects.create(employee_number="E7", full_name="Old")
    admin.delete(f"{DEVELOPERS}{old.pk}/")
    Developer.objects.create(employee_number="E7", full_name="New")
    r = admin.post(f"{DEVELOPERS}{old.pk}/restore/")
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert Developer.all_objects.get(pk=old.pk).deleted_at is not None


@pytest.mark.django_db
def test_only_those_who_may_delete(auth_client, make_user):
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    ada.soft_delete()
    boss = auth_client(make_user(Roles.BOSS))
    assert boss.get(f"{DEVELOPERS}deleted/").status_code == 403
    assert boss.post(f"{DEVELOPERS}{ada.pk}/restore/").status_code == 403


@pytest.mark.django_db
def test_restore_with_the_old_card(auth_client, make_user):
    admin = auth_client(make_user(Roles.ADMIN))
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    card = RFIDCard.objects.create(uid="04AA", label="0001")
    RFIDCardAssignment.objects.create(card=card, developer=ada)
    admin.delete(f"{DEVELOPERS}{ada.pk}/")
    row = admin.get(f"{DEVELOPERS}deleted/").json()["results"][0]
    assert row["old_card"] == {"uid": "04AA", "label": "0001", "state": "FREE"}
    r = admin.post(f"{DEVELOPERS}{ada.pk}/restore/", {"card": True}, format="json")
    assert (r.json()["card_restored"], r.json()["card_label"]) == (True, "0001")
    assert RFIDCardAssignment.objects.filter(
        developer=ada, card=card, unassigned_at__isnull=True
    ).exists()


@pytest.mark.django_db
def test_old_card_given_to_someone_else_is_not_taken_back(auth_client, make_user):
    admin = auth_client(make_user(Roles.ADMIN))
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    bob = Developer.objects.create(employee_number="E2", full_name="Bob")
    card = RFIDCard.objects.create(uid="04AA")
    RFIDCardAssignment.objects.create(card=card, developer=ada)
    admin.delete(f"{DEVELOPERS}{ada.pk}/")
    RFIDCardAssignment.objects.create(card=card, developer=bob)
    assert admin.get(f"{DEVELOPERS}deleted/").json()["results"][0]["old_card"]["state"] == "TAKEN"
    r = admin.post(f"{DEVELOPERS}{ada.pk}/restore/", {"card": True}, format="json")
    assert r.status_code == 200 and r.json()["card_restored"] is False
    assert RFIDCardAssignment.objects.get(card=card, unassigned_at__isnull=True).developer == bob


@pytest.mark.django_db
def test_give_back_the_last_card_after_an_unassign(auth_client, make_user):
    from apps.rfid import services as rfid

    admin = auth_client(make_user(Roles.ADMIN))
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    card = RFIDCard.objects.create(uid="04AA", label="0001")
    rfid.assign_card(actor=None, card=card, developer=ada)
    rfid.unassign_card(actor=None, card=card)
    assert admin.get(f"{DEVELOPERS}{ada.pk}/last-card/").json()["card"]["state"] == "FREE"
    r = admin.post(f"{DEVELOPERS}{ada.pk}/restore-card/")
    assert r.status_code == 200 and r.json()["label"] == "0001"
    again = admin.post(f"{DEVELOPERS}{ada.pk}/restore-card/").json()
    assert again["error"]["code"] == "VALIDATION_ERROR"  # has a card now
    # A last card retired since is restored and given back.
    rfid.unassign_card(actor=None, card=card)
    rfid.change_card_status(actor=None, card=card, transition="retire")
    assert admin.get(f"{DEVELOPERS}{ada.pk}/last-card/").json()["card"]["state"] == "RETIRED"
    r = admin.post(f"{DEVELOPERS}{ada.pk}/restore-card/")
    assert r.status_code == 200
    card.refresh_from_db()
    assert card.status == "ACTIVE"
    assert RFIDCardAssignment.objects.get(card=card, unassigned_at__isnull=True).developer == ada
    nobody = Developer.objects.create(employee_number="E2", full_name="Bob")
    assert admin.get(f"{DEVELOPERS}{nobody.pk}/last-card/").json() == {"card": None}


@pytest.mark.django_db
def test_terminated_gets_a_last_working_day(auth_client, make_user):
    from datetime import date

    from django.utils import timezone

    admin = auth_client(make_user(Roles.ADMIN))
    ada = Developer.objects.create(employee_number="E1", full_name="Ada")
    r = admin.patch(f"{DEVELOPERS}{ada.pk}/", {"status": "TERMINATED"}, format="json")
    assert r.json()["out_date"] == timezone.localdate().isoformat()
    # A day given (or already there) is kept.
    bob = Developer.objects.create(employee_number="E2", full_name="Bob")
    r = admin.patch(
        f"{DEVELOPERS}{bob.pk}/", {"status": "TERMINATED", "out_date": "2026-09-30"}, format="json"
    )
    assert r.json()["out_date"] == "2026-09-30"
    assert Developer.objects.get(pk=bob.pk).out_date == date(2026, 9, 30)


@pytest.mark.django_db
def test_terminated_needs_an_empty_wallet_and_closes_it(auth_client, make_user):
    from decimal import Decimal

    from apps.finance import services as finance
    from apps.finance.models import AccountStatus, DeveloperAccount

    admin = auth_client(make_user(Roles.ADMIN))
    rich = Developer.objects.create(employee_number="E1", full_name="Rich")
    finance.deposit(actor=None, developer=rich, amount="25.00", idempotency_key="d-1")
    r = admin.patch(f"{DEVELOPERS}{rich.pk}/", {"status": "TERMINATED"}, format="json")
    assert r.status_code == 409, r.json()
    assert r.json()["error"]["details"]["balance"] == "25.00"
    rich.refresh_from_db()
    assert (rich.status, rich.out_date) == ("ACTIVE", None)  # nothing changed

    # Paid out (adjusted to 0): now Terminated works and the wallet is closed.
    finance.adjust(
        actor=None, developer=rich, amount="-25.00", reason="Paid out", idempotency_key="p-1"
    )
    r = admin.patch(f"{DEVELOPERS}{rich.pk}/", {"status": "TERMINATED"}, format="json")
    assert r.status_code == 200, r.json()
    account = DeveloperAccount.objects.get(developer=rich)
    assert (account.balance, account.status) == (Decimal("0.00"), AccountStatus.CLOSED)

    # No wallet at all: fine too.
    nobody = Developer.objects.create(employee_number="E2", full_name="Nobody")
    assert (
        admin.patch(
            f"{DEVELOPERS}{nobody.pk}/", {"status": "TERMINATED"}, format="json"
        ).status_code
        == 200
    )
