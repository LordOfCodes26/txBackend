import io
import threading

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from PIL import Image

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.goods import services
from apps.goods.exceptions import InsufficientStock
from apps.goods.models import Good, InventoryMovement
from apps.sellers.models import Seller, ServicePosition

GOODS = "/api/v1/goods/"
MOVEMENTS = "/api/v1/inventory/movements/"


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def position(db):
    return ServicePosition.objects.create(seller=Seller.objects.create(name="Cafe"), name="Counter")


@pytest.fixture
def seller_manager(auth_client, make_user):
    return auth_client(make_user(Roles.ADMIN))


@pytest.fixture
def make_good(position):
    def _make(**extra):
        data = {"service_position": position, "name": "Tea", "price": "2.50"} | extra
        quantity = data.pop("quantity", 0)
        good = Good.objects.create(**data)
        if quantity:
            with transaction.atomic():
                services.move_stock(good=good, kind="INITIAL_STOCK", delta=quantity)
            good.refresh_from_db()
        return good

    return _make


def png(size=(10, 10)) -> SimpleUploadedFile:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format="PNG")
    return SimpleUploadedFile("pic.png", buf.getvalue(), content_type="image/png")


# --- Goods CRUD -----------------------------------------------------------------


@pytest.mark.django_db
def test_create_good_with_initial_stock(seller_manager, position):
    response = seller_manager.post(
        GOODS,
        {
            "service_position": position.pk,
            "name": "Coffee",
            "price": "3.20",
            "initial_quantity": 40,
        },
    )
    assert response.status_code == 201, response.json()
    body = response.json()
    assert (body["price"], body["quantity"], body["currency"]) == ("3.20", 40, "USD")
    assert body["seller"]["name"] == "Cafe"
    movement = InventoryMovement.objects.get()
    assert (movement.kind, movement.quantity_delta, movement.quantity_after) == (
        "INITIAL_STOCK",
        40,
        40,
    )
    assert {"good.created", "good.stock_changed"} <= set(
        AuditLog.objects.values_list("action", flat=True)
    )


@pytest.mark.django_db
def test_price_must_be_non_negative_decimal(seller_manager, position):
    for price in ("-1", "abc", "1.234"):
        response = seller_manager.post(
            GOODS, {"service_position": position.pk, "name": "X", "price": price}
        )
        assert response.status_code == 400, price


@pytest.mark.django_db
def test_quantity_cannot_be_set_directly(seller_manager, make_good):
    good = make_good(quantity=5)
    seller_manager.patch(f"{GOODS}{good.pk}/", {"quantity": 999})
    good.refresh_from_db()
    assert good.quantity == 5
    response = seller_manager.patch(f"{GOODS}{good.pk}/", {"initial_quantity": 9})
    assert response.status_code == 400


@pytest.mark.django_db
def test_sku_unique_per_seller(seller_manager, make_good, position):
    make_good(sku="TEA-1")
    response = seller_manager.post(
        GOODS, {"service_position": position.pk, "name": "Tea 2", "price": "1", "sku": "tea-1"}
    )
    assert "sku" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_update_audits_price_change(seller_manager, make_good):
    good = make_good(price="2.50")
    seller_manager.patch(f"{GOODS}{good.pk}/", {"price": "2.75"})
    log = AuditLog.objects.get(action="good.updated")
    assert (log.old_values, log.new_values) == ({"price": "2.50"}, {"price": "2.75"})


@pytest.mark.django_db
def test_soft_delete_hides_good_and_keeps_history(seller_manager, make_good):
    good = make_good(quantity=3)
    assert seller_manager.delete(f"{GOODS}{good.pk}/").status_code == 204
    assert seller_manager.get(f"{GOODS}{good.pk}/").status_code == 404
    assert Good.all_objects.get(pk=good.pk).is_active is False
    assert InventoryMovement.objects.filter(good=good).exists()


@pytest.mark.django_db
def test_good_filters(seller_manager, make_good):
    make_good(name="Tea", quantity=3, price="2.00")
    make_good(name="Cake", price="5.00")  # tracked, 0 stock
    make_good(name="Haircut", track_stock=False, price="15.00")

    def names(query):
        return sorted(g["name"] for g in seller_manager.get(f"{GOODS}?{query}").json()["results"])

    assert names("in_stock=true") == ["Haircut", "Tea"]
    assert names("in_stock=false") == ["Cake"]
    assert names("price_min=4&price_max=10") == ["Cake"]
    assert names("search=hair") == ["Haircut"]


# --- Seller scoping -------------------------------------------------------------


@pytest.fixture
def seller_client(auth_client, make_user, position):
    user = make_user(Roles.SELLER)
    Seller.objects.filter(pk=position.seller_id).update(user=user)
    return auth_client(user)


@pytest.mark.django_db
def test_seller_manages_only_own_goods(seller_client, make_good, position):
    mine = make_good(name="Mine", quantity=2)
    other_position = ServicePosition.objects.create(
        seller=Seller.objects.create(name="Other"), name="P"
    )
    theirs = make_good(name="Theirs", service_position=other_position, quantity=2)

    assert [g["name"] for g in seller_client.get(GOODS).json()["results"]] == ["Mine"]
    assert seller_client.get(f"{GOODS}{theirs.pk}/").status_code == 404
    assert seller_client.patch(f"{GOODS}{theirs.pk}/", {"price": "0"}).status_code == 404
    assert (
        seller_client.post(
            f"{GOODS}{theirs.pk}/stock/", {"kind": "RESTOCK", "quantity": 1}
        ).status_code
        == 404
    )

    response = seller_client.post(
        GOODS, {"service_position": other_position.pk, "name": "Sneaky", "price": "1"}
    )
    assert response.status_code == 400
    assert seller_client.patch(f"{GOODS}{mine.pk}/", {"price": "9.99"}).status_code == 200
    assert (
        seller_client.post(
            f"{GOODS}{mine.pk}/stock/", {"kind": "RESTOCK", "quantity": 1}
        ).status_code
        == 201
    )
    movements = seller_client.get(MOVEMENTS).json()["results"]
    assert {m["good"] for m in movements} == {mine.pk}


@pytest.mark.django_db
def test_good_cannot_move_to_another_seller(seller_manager, make_good):
    good = make_good()
    other = ServicePosition.objects.create(seller=Seller.objects.create(name="O"), name="P")
    response = seller_manager.patch(f"{GOODS}{good.pk}/", {"service_position": other.pk})
    assert response.status_code == 400


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("role", "view", "stock"),
    [
        (Roles.ADMIN, 200, 201),
        (Roles.MANAGER, 200, 403),
        (Roles.FINANCE_MANAGER, 403, 403),
        (Roles.DEVELOPER, 403, 403),
    ],
)
def test_goods_access_by_role(auth_client, make_user, make_good, role, view, stock):
    client = auth_client(make_user(role))
    good = make_good()
    assert client.get(GOODS).status_code == view
    assert (
        client.post(f"{GOODS}{good.pk}/stock/", {"kind": "RESTOCK", "quantity": 1}).status_code
        == stock
    )


# --- Stock ------------------------------------------------------------------------


@pytest.mark.django_db
def test_restock_damage_and_adjust(seller_manager, make_good):
    good = make_good(quantity=10)
    url = f"{GOODS}{good.pk}/stock/"

    assert (
        seller_manager.post(url, {"kind": "RESTOCK", "quantity": 5}).json()["quantity_after"] == 15
    )
    r = seller_manager.post(url, {"kind": "DAMAGE", "quantity": 2, "reason": "Dropped"})
    assert (r.json()["quantity_delta"], r.json()["quantity_after"]) == (-2, 13)
    r = seller_manager.post(url, {"kind": "ADJUSTMENT", "counted_quantity": 11, "reason": "Count"})
    assert (r.json()["quantity_delta"], r.json()["quantity_after"]) == (-2, 11)

    good.refresh_from_db()
    assert good.quantity == 11
    assert services.stock_mismatches() == []


@pytest.mark.django_db
def test_stock_validation(seller_manager, make_good):
    good = make_good(quantity=3)
    url = f"{GOODS}{good.pk}/stock/"

    r = seller_manager.post(url, {"kind": "DAMAGE", "quantity": 5, "reason": "x"})
    assert r.status_code == 409
    assert r.json()["error"] == {
        "code": "INSUFFICIENT_STOCK",
        "message": "Not enough stock for this change.",
        "details": {"available": 3, "requested": 5},
    }
    assert seller_manager.post(url, {"kind": "DAMAGE", "quantity": 1}).status_code == 400
    assert seller_manager.post(url, {"kind": "RESTOCK", "quantity": 0}).status_code == 400
    assert seller_manager.post(url, {"kind": "SALE", "quantity": 1}).status_code == 400
    r = seller_manager.post(url, {"kind": "ADJUSTMENT", "counted_quantity": 3, "reason": "x"})
    assert r.json()["error"]["code"] == "NO_STOCK_CHANGE"


@pytest.mark.django_db
def test_untracked_good_rejects_stock_changes(seller_manager, make_good):
    good = make_good(track_stock=False)
    r = seller_manager.post(f"{GOODS}{good.pk}/stock/", {"kind": "RESTOCK", "quantity": 1})
    assert r.json()["error"]["code"] == "STOCK_NOT_TRACKED"


@pytest.mark.django_db
def test_movements_are_append_only(make_good):
    make_good(quantity=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("UPDATE goods_inventorymovement SET quantity_delta = 100")


@pytest.mark.django_db
def test_database_rejects_negative_quantity(make_good):
    good = make_good()
    with pytest.raises(IntegrityError), transaction.atomic():
        Good.objects.filter(pk=good.pk).update(quantity=-1)


@pytest.mark.django_db
def test_check_inventory_detects_drift(make_good):
    good = make_good(quantity=5)
    assert services.stock_mismatches() == []
    Good.objects.filter(pk=good.pk).update(quantity=7)  # bypassing the ledger
    assert services.stock_mismatches() == [
        {"id": good.pk, "name": "Tea", "quantity": 7, "ledger": 5}
    ]


@pytest.mark.django_db(transaction=True)
def test_concurrent_stock_decrements_never_oversell(make_good):
    good = make_good(quantity=5)
    barrier = threading.Barrier(8)
    outcomes = []

    def take_one():
        try:
            barrier.wait()
            with transaction.atomic():
                services.move_stock(good=good, kind="SALE", delta=-1)
            outcomes.append("ok")
        except InsufficientStock:
            outcomes.append("short")
        finally:
            connection.close()

    threads = [threading.Thread(target=take_one) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(outcomes) == ["ok"] * 5 + ["short"] * 3
    good.refresh_from_db()
    assert good.quantity == 0
    assert services.stock_mismatches() == []


# --- Images -----------------------------------------------------------------------


@pytest.mark.django_db
def test_upload_and_delete_image(seller_manager, make_good, settings):
    good = make_good()
    r = seller_manager.post(
        f"{GOODS}{good.pk}/images/", {"image": png(), "alt_text": "Red"}, format="multipart"
    )
    assert r.status_code == 201, r.json()
    image = r.json()["images"][0]
    assert image["image"].startswith("http://testserver/media/goods/")
    stored = list(settings.MEDIA_ROOT.rglob("*.png"))
    assert len(stored) == 1

    with django_capture_on_commit():
        assert seller_manager.delete(f"{GOODS}{good.pk}/images/{image['id']}/").status_code == 204
    assert not stored[0].exists()


def django_capture_on_commit():
    """Run on_commit callbacks inside a test transaction."""
    from django.test import TestCase

    return TestCase.captureOnCommitCallbacks(execute=True)


@pytest.mark.django_db
def test_image_validation(seller_manager, make_good, settings):
    good = make_good()
    url = f"{GOODS}{good.pk}/images/"
    not_image = SimpleUploadedFile("x.png", b"not an image", content_type="image/png")
    assert seller_manager.post(url, {"image": not_image}, format="multipart").status_code == 400

    settings.GOOD_IMAGE_MAX_BYTES = 10
    assert seller_manager.post(url, {"image": png()}, format="multipart").status_code == 400

    settings.GOOD_IMAGE_MAX_BYTES = 5 * 1024 * 1024
    settings.GOOD_MAX_IMAGES = 1
    assert seller_manager.post(url, {"image": png()}, format="multipart").status_code == 201
    r = seller_manager.post(url, {"image": png()}, format="multipart")
    assert r.json()["error"]["code"] == "TOO_MANY_IMAGES"
