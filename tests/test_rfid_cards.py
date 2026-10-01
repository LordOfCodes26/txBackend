import pytest
from django.db import IntegrityError, transaction

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer, DeveloperStatus
from apps.rfid.models import CardStatus, RFIDCard, RFIDCardAssignment

pytestmark = pytest.mark.django_db

CARDS = "/api/v1/rfid/cards/"


@pytest.fixture
def make_developer():
    counter = iter(range(1, 100_000))

    def _make(**extra):
        n = next(counter)
        data = {"employee_number": f"E{n:05d}", "full_name": f"Dev {n}"}
        return Developer.objects.create(**(data | extra))

    return _make


@pytest.fixture
def make_card():
    counter = iter(range(1, 100_000))

    def _make(**extra):
        return RFIDCard.objects.create(**({"uid": f"04AA{next(counter):06X}"} | extra))

    return _make


@pytest.fixture
def client(auth_client, make_user):
    return auth_client(make_user(Roles.MANAGER))


def test_register_card_normalizes_uid(client):
    response = client.post(CARDS, {"uid": "04:aa:bb:cc", "label": "001"})
    assert response.status_code == 201, response.json()
    assert response.json()["uid"] == "04AABBCC"
    assert response.json()["current_assignment"] is None
    assert AuditLog.objects.filter(action="rfid.card_registered").exists()


def test_register_duplicate_or_invalid_uid(client, make_card):
    make_card(uid="04AABBCC")
    assert client.post(CARDS, {"uid": "04-aa-bb-cc"}).status_code == 400
    assert client.post(CARDS, {"uid": "not-hex!"}).status_code == 400


def test_assign_and_unassign(client, make_card, make_developer):
    card, dev = make_card(), make_developer()

    response = client.post(f"{CARDS}{card.pk}/assign/", {"developer": dev.pk})
    assert response.status_code == 200
    assert response.json()["current_assignment"]["developer"]["id"] == dev.pk

    response = client.post(f"{CARDS}{card.pk}/unassign/")
    assert response.status_code == 200
    assert response.json()["current_assignment"] is None
    assignment = RFIDCardAssignment.objects.get()
    assert assignment.end_reason == "RETURNED" and assignment.unassigned_at is not None
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"rfid.card_assigned", "rfid.card_unassigned"} <= actions


def test_card_cannot_have_two_owners(client, make_card, make_developer):
    card = make_card()
    client.post(f"{CARDS}{card.pk}/assign/", {"developer": make_developer().pk})
    response = client.post(f"{CARDS}{card.pk}/assign/", {"developer": make_developer().pk})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CARD_ALREADY_ASSIGNED"


def test_developer_cannot_have_two_cards(client, make_card, make_developer):
    dev = make_developer()
    client.post(f"{CARDS}{make_card().pk}/assign/", {"developer": dev.pk})
    response = client.post(f"{CARDS}{make_card().pk}/assign/", {"developer": dev.pk})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEVELOPER_ALREADY_HAS_CARD"


def test_database_enforces_one_active_assignment(make_card, make_developer):
    card, dev = make_card(), make_developer()
    RFIDCardAssignment.objects.create(card=card, developer=dev)
    with pytest.raises(IntegrityError), transaction.atomic():
        RFIDCardAssignment.objects.create(card=card, developer=make_developer())
    with pytest.raises(IntegrityError), transaction.atomic():
        RFIDCardAssignment.objects.create(card=make_card(), developer=dev)


def test_cannot_assign_to_terminated_developer(client, make_card, make_developer):
    dev = make_developer(status=DeveloperStatus.TERMINATED)
    response = client.post(f"{CARDS}{make_card().pk}/assign/", {"developer": dev.pk})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DEVELOPER_NOT_ASSIGNABLE"


def test_replace_card_keeps_history(client, make_card, make_developer):
    old, dev = make_card(), make_developer()
    client.post(f"{CARDS}{old.pk}/assign/", {"developer": dev.pk})

    response = client.post(
        f"{CARDS}{old.pk}/replace/", {"new_card_uid": "04 ff ee dd", "reason": "Lost"}
    )
    assert response.status_code == 200, response.json()
    assert response.json()["uid"] == "04FFEEDD"
    assert response.json()["current_assignment"]["developer"]["id"] == dev.pk

    old.refresh_from_db()
    assert old.status == CardStatus.RETIRED and old.status_reason == "Lost"
    history = list(
        RFIDCardAssignment.objects.filter(developer=dev)
        .order_by("id")
        .values_list("card__uid", "end_reason")
    )
    assert history == [(old.uid, "REPLACED"), ("04FFEEDD", "")]
    assert AuditLog.objects.filter(action="rfid.card_replaced", entity_id=str(dev.pk)).exists()


def test_replace_with_already_assigned_card_rolls_back(client, make_card, make_developer):
    old, other = make_card(), make_card()
    client.post(f"{CARDS}{old.pk}/assign/", {"developer": make_developer().pk})
    client.post(f"{CARDS}{other.pk}/assign/", {"developer": make_developer().pk})

    response = client.post(f"{CARDS}{old.pk}/replace/", {"new_card_uid": other.uid})
    assert response.status_code == 409
    old.refresh_from_db()
    assert old.status == CardStatus.ACTIVE
    assert old.current_assignment is not None


def test_block_keeps_owner_and_unblock_restores(auth_client, make_user, make_card, make_developer):
    client = auth_client(make_user(Roles.MANAGER))
    card = make_card()
    client.post(f"{CARDS}{card.pk}/assign/", {"developer": make_developer().pk})

    body = client.post(f"{CARDS}{card.pk}/block/", {"reason": "Reported lost"}).json()
    assert body["status"] == "BLOCKED" and body["current_assignment"] is not None
    assert client.post(f"{CARDS}{card.pk}/block/").status_code == 409
    assert client.post(f"{CARDS}{card.pk}/unblock/").json()["status"] == "ACTIVE"


def test_retire_requires_unassigned_card(client, make_card, make_developer):
    card = make_card()
    client.post(f"{CARDS}{card.pk}/assign/", {"developer": make_developer().pk})
    assert client.post(f"{CARDS}{card.pk}/retire/").status_code == 409
    client.post(f"{CARDS}{card.pk}/unassign/")
    assert client.post(f"{CARDS}{card.pk}/retire/").json()["status"] == "RETIRED"
    response = client.post(f"{CARDS}{card.pk}/assign/", {"developer": make_developer().pk})
    assert response.json()["error"]["code"] == "CARD_NOT_ACTIVE"


def test_terminating_developer_ends_assignment(client, make_card, make_developer):
    card, dev = make_card(), make_developer()
    client.post(f"{CARDS}{card.pk}/assign/", {"developer": dev.pk})
    client.patch(f"/api/v1/developers/{dev.pk}/", {"status": "TERMINATED"})
    assert RFIDCardAssignment.objects.get().end_reason == "DEVELOPER_LEFT"


def test_deleting_developer_ends_assignment(client, make_card, make_developer):
    card, dev = make_card(), make_developer()
    client.post(f"{CARDS}{card.pk}/assign/", {"developer": dev.pk})
    assert client.delete(f"/api/v1/developers/{dev.pk}/").status_code == 204
    assert RFIDCardAssignment.objects.get().end_reason == "DEVELOPER_DELETED"


def test_card_filters(client, make_card, make_developer):
    assigned = make_card(label="A")
    make_card(label="F")
    dev = make_developer(full_name="Zed Unique")
    client.post(f"{CARDS}{assigned.pk}/assign/", {"developer": dev.pk})

    def labels(query):
        return [c["label"] for c in client.get(f"{CARDS}?{query}").json()["results"]]

    assert labels("assigned=true") == ["A"]
    assert labels("assigned=false") == ["F"]
    assert labels(f"developer={dev.pk}") == ["A"]
    assert labels("search=Zed") == ["A"]


@pytest.mark.parametrize(
    ("role", "view", "assign", "block"),
    [
        (Roles.MANAGER, 200, 200, 200),
        (Roles.FINANCE_MANAGER, 403, 403, 403),
        (Roles.DEVELOPER, 403, 403, 403),
        (Roles.SELLER, 403, 403, 403),
    ],
)
def test_card_permissions(
    auth_client, make_user, make_card, make_developer, role, view, assign, block
):
    client = auth_client(make_user(role))
    card = make_card()
    assert client.get(CARDS).status_code == view
    r = client.post(f"{CARDS}{card.pk}/assign/", {"developer": make_developer().pk})
    assert r.status_code == assign
    assert client.post(f"{CARDS}{card.pk}/block/").status_code == block


def test_cards_cannot_be_deleted(client, make_card):
    card = make_card()
    assert client.delete(f"{CARDS}{card.pk}/").status_code in (403, 405)
