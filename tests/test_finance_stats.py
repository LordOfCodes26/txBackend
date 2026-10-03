"""Finance statistics (/stats/finance/): money and a per-seller comparison."""

import pytest

from apps.accounts.rbac import Roles
from tests.test_building_owners import site  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db

FINANCE_STATS = "/api/v1/stats/finance/"


def by_name(body):
    return {row["name"]: row for row in body["sellers"]}


def test_finance_manager_compares_all_sellers(auth_client, make_user, site):  # noqa: F811
    client = auth_client(make_user(Roles.FINANCE_MANAGER))
    assert client.get("/api/v1/stats/").status_code == 403  # company stats stay closed
    body = client.get(FINANCE_STATS).json()
    assert body["buildings"] is None
    assert "people" not in body
    assert body["money"]["deposits"]["total"] == "400.00"
    assert [row["name"] for row in body["sellers"]] == ["Cafe", "Shop"]  # by sales, desc
    cafe = by_name(body)["Cafe"]
    assert (cafe["sales_total"], cafe["sales_count"]) == ("50.00", 2)
    assert (cafe["earnings"], cafe["payouts_paid"], cafe["balance"]) == ("50.00", "0.00", "50.00")
    assert (cafe["bookings_total"], cafe["payouts_pending"]) == ("0.00", "9.00")


def test_building_owner_sees_own_sellers(auth_client, site):  # noqa: F811
    body = auth_client(site.owner).get(FINANCE_STATS).json()
    assert [b["code"] for b in body["buildings"]] == ["B1"]
    sellers = by_name(body)
    assert sellers["Cafe"]["sales_total"] == "10.00"  # only the Building 1 counter
    assert sellers["Cafe"]["earnings"] is None  # store spans two buildings
    assert (sellers["Shop"]["sales_total"], sellers["Shop"]["earnings"]) == ("5.00", "5.00")


def test_needs_finance_and_seller_finance_view(auth_client, make_user):
    assert auth_client(make_user(Roles.MANAGER)).get(FINANCE_STATS).status_code == 403
    assert auth_client(make_user(Roles.BOSS)).get(FINANCE_STATS).status_code == 200
