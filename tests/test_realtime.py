"""WebSocket notifications for a counter, end to end through the ASGI app."""

import uuid

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from channels.testing import WebsocketCommunicator
from rest_framework.test import APIClient

from apps.accounts.rbac import Roles
from apps.developers.models import Developer
from apps.finance import services as finance
from apps.goods.models import Good
from apps.purchases import services as purchases
from apps.realtime.tickets import issue_ticket
from apps.rfid import services as rfid
from apps.rfid.models import RFIDCard, RFIDCardAssignment
from apps.sellers.models import Seller, ServicePosition
from config.asgi import application

pytestmark = pytest.mark.django_db(transaction=True)
PIN = "4826"


@pytest.fixture
def shop(make_user):
    seller_user = make_user(Roles.SELLER, email="cafe@x.com")
    seller = Seller.objects.create(name="Cafe", user=seller_user)
    counter = ServicePosition.objects.create(seller=seller, name="Counter 1")
    tea = Good.objects.create(
        service_position=counter, name="Tea", price="2.50", kind="SERVICE", track_stock=False
    )
    till, till_key = rfid.register_device(
        actor=None, code="TILL-1", purpose="TILL", service_position=counter
    )
    dev = Developer.objects.create(employee_number="E1", full_name="Ada Lovelace")
    card = RFIDCard.objects.create(uid="04AA000001")
    RFIDCardAssignment.objects.create(card=card, developer=dev)
    account = finance.open_account(dev)
    finance.deposit(actor=None, developer=dev, amount="20.00", idempotency_key="seed-000001")
    finance.set_pin(actor=None, account=account, pin=PIN, current_pin=None)
    return {
        "seller_user": seller_user,
        "counter": counter,
        "tea": tea,
        "till": till,
        "till_key": till_key,
        "card": card,
        "dev": dev,
    }


def path(counter, ticket=None):
    return f"/ws/counters/{counter.pk}/" + (f"?ticket={ticket}" if ticket else "")


async def open_ws(url, headers=None):
    comm = WebsocketCommunicator(application, url, headers=headers or [])
    connected, _ = await comm.connect()
    assert connected
    first = await comm.receive_output(timeout=3)
    return comm, first


async def expect_close(url, headers=None):
    comm, first = await open_ws(url, headers)
    await comm.disconnect()
    assert first["type"] == "websocket.close", first
    return first["code"]


def run(coro_fn):
    return async_to_sync(coro_fn)()


# --- Events reach the seller's screen ----------------------------------------------------


def test_seller_screen_receives_tap_update_and_confirmation(shop):
    ticket = issue_ticket(shop["seller_user"])

    async def scenario():
        comm, hello = await open_ws(path(shop["counter"], ticket))
        assert '"connected"' in hello["text"]

        purchase = await sync_to_async(purchases.create_purchase)(
            actor=shop["seller_user"], service_position=shop["counter"]
        )
        await sync_to_async(purchases.add_item)(purchase=purchase, good=shop["tea"], quantity=2)
        updated = await comm.receive_json_from(timeout=3)
        assert updated["type"] == "purchase_updated"
        assert (updated["data"]["id"], updated["data"]["total"]) == (purchase.pk, "5.00")

        await sync_to_async(rfid.record_scan)(device=shop["till"], uid=shop["card"].uid)
        tapped = await comm.receive_json_from(timeout=3)
        assert tapped["type"] == "card_tapped"
        data = tapped["data"]
        assert (data["accepted"], data["purchase"]) == (True, purchase.pk)
        assert data["developer"]["full_name"] == "Ada Lovelace"
        assert data["display_message"] == "Ada Lovelace - enter PIN"

        await sync_to_async(purchases.confirm_purchase)(
            actor=shop["seller_user"], purchase=purchase, pin=PIN, idempotency_key=str(uuid.uuid4())
        )
        confirmed = await comm.receive_json_from(timeout=3)
        assert confirmed["type"] == "purchase_confirmed"
        assert (confirmed["data"]["status"], confirmed["data"]["balance_after"]) == (
            "CONFIRMED",
            "15.00",
        )
        await comm.disconnect()

    run(scenario)


def test_rejected_tap_is_pushed_too(shop):
    RFIDCard.objects.filter(pk=shop["card"].pk).update(status="BLOCKED")
    ticket = issue_ticket(shop["seller_user"])

    async def scenario():
        comm, _ = await open_ws(path(shop["counter"], ticket))
        await sync_to_async(rfid.record_scan)(device=shop["till"], uid=shop["card"].uid)
        tapped = await comm.receive_json_from(timeout=3)
        assert (tapped["data"]["accepted"], tapped["data"]["display_message"]) == (
            False,
            "Card blocked",
        )
        assert "uid" not in tapped["data"] or tapped["data"].get("uid") is None
        await comm.disconnect()

    run(scenario)


def test_till_program_can_listen_with_its_device_key(shop):
    headers = [(b"authorization", f"Device {shop['till_key']}".encode())]

    async def scenario():
        comm, hello = await open_ws(path(shop["counter"]), headers)
        assert '"device:TILL-1"' in hello["text"]
        purchase = await sync_to_async(purchases.create_purchase)(
            actor=shop["seller_user"], service_position=shop["counter"]
        )
        await sync_to_async(purchases.cancel_purchase)(actor=shop["seller_user"], purchase=purchase)
        event = await comm.receive_json_from(timeout=3)
        assert (event["type"], event["data"]["status"]) == ("purchase_cancelled", "CANCELLED")
        await comm.disconnect()

    run(scenario)


def test_other_counters_do_not_receive_events(shop):
    other = ServicePosition.objects.create(seller=shop["counter"].seller, name="Counter 2")
    ticket = issue_ticket(shop["seller_user"])

    async def scenario():
        comm, _ = await open_ws(path(other, ticket))
        await sync_to_async(rfid.record_scan)(device=shop["till"], uid=shop["card"].uid)
        assert await comm.receive_nothing(timeout=0.5)
        await comm.disconnect()

    run(scenario)


# --- Access control --------------------------------------------------------------------


def test_connection_rules(shop, make_user):
    other_seller_user = make_user(Roles.SELLER)
    Seller.objects.create(name="Other", user=other_seller_user)
    other_counter = ServicePosition.objects.create(
        seller=Seller.objects.get(name="Other"), name="P"
    )
    _, other_till_key = rfid.register_device(
        actor=None, code="TILL-2", purpose="TILL", service_position=other_counter
    )
    staff = make_user(Roles.SELLER_MANAGER)  # has purchase.view
    used = issue_ticket(shop["seller_user"])

    async def scenario():
        counter = shop["counter"]
        comm, _ = await open_ws(path(counter, used))
        await comm.disconnect()
        assert await expect_close(path(counter, used)) == 4401  # tickets work once
        assert await expect_close(path(counter, "made-up")) == 4401
        assert await expect_close(path(counter)) == 4401
        t = await sync_to_async(issue_ticket)(other_seller_user)
        assert await expect_close(path(counter, t)) == 4403  # another seller's counter
        bad_device = [(b"authorization", f"Device {other_till_key}".encode())]
        assert await expect_close(path(counter), bad_device) == 4403
        t = await sync_to_async(issue_ticket)(staff)
        comm, hello = await open_ws(path(counter, t))  # staff may watch any counter
        assert '"connected"' in hello["text"]
        await comm.disconnect()
        t = await sync_to_async(issue_ticket)(staff)
        assert await expect_close(f"/ws/counters/999999/?ticket={t}") == 4404

    run(scenario)


def test_ticket_endpoint(shop):
    client = APIClient()
    assert client.post("/api/v1/realtime/ticket/").status_code == 401
    client.force_authenticate(shop["seller_user"])
    body = client.post("/api/v1/realtime/ticket/").json()
    assert body["expires_in"] == 30 and len(body["ticket"]) > 30


# --- Robustness -------------------------------------------------------------------------


def test_purchase_succeeds_when_realtime_is_down(shop, monkeypatch):
    class BrokenLayer:
        async def group_send(self, *args, **kwargs):
            raise ConnectionError("redis down")

    monkeypatch.setattr("apps.realtime.notify.get_channel_layer", lambda: BrokenLayer())
    purchase = purchases.create_purchase(
        actor=shop["seller_user"], service_position=shop["counter"]
    )
    purchases.add_item(purchase=purchase, good=shop["tea"], quantity=1)
    rfid.record_scan(device=shop["till"], uid=shop["card"].uid)
    purchase, created = purchases.confirm_purchase(
        actor=shop["seller_user"], purchase=purchase, pin=PIN, idempotency_key=str(uuid.uuid4())
    )
    assert created and purchase.status == "CONFIRMED"
