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
    assert cafe["bookings_total"] == "0.00"
    assert "balance" not in cafe and "payouts_paid" not in cafe


def test_building_owner_sees_what_their_buyers_bought(auth_client, site):  # noqa: F811
    """By the BUYER's building: Ada (Building 1) bought at Cafe's counters in both
    buildings; all of it counts for Building 1."""
    body = auth_client(site.owner).get(FINANCE_STATS).json()
    assert [b["code"] for b in body["buildings"]] == ["B1"]
    sellers = by_name(body)
    assert sellers["Cafe"]["sales_total"] == "50.00"
    assert sellers["Shop"]["sales_total"] == "5.00"
    assert body["money"]["store_sales"]["sales_total"] == "55.00"


def test_needs_finance_view(auth_client, make_user):
    assert auth_client(make_user(Roles.MANAGER)).get(FINANCE_STATS).status_code == 403
    assert auth_client(make_user(Roles.BOSS)).get(FINANCE_STATS).status_code == 200


def test_developers_by_deposits(auth_client, make_user, site):  # noqa: F811
    from apps.finance import services as finance

    for i in range(2):  # Ada: more deposits, Bob: a bigger one
        finance.deposit(actor=None, developer=site.ada, amount="10.00", idempotency_key=f"x-{i}")
    body = auth_client(make_user(Roles.FINANCE_MANAGER)).get(FINANCE_STATS).json()
    rows = body["developers"]
    assert [r["employee_number"] for r in rows] == ["E2", "E1"]  # by deposit total
    bob, ada = rows
    assert (bob["deposits"], bob["deposit_count"], bob["spending"]) == ("300.00", 1, "0.00")
    assert (ada["deposits"], ada["deposit_count"]) == ("120.00", 3)
    assert (ada["spending"], ada["spending_count"], ada["balance"]) == ("55.00", 3, "65.00")
    assert (ada["full_name"], ada["building"], ada["building_name"]) == ("Ada", "B1", "Building 1")


def test_building_owner_sees_own_developers_only(auth_client, site):  # noqa: F811
    rows = auth_client(site.owner).get(FINANCE_STATS).json()["developers"]
    assert [r["employee_number"] for r in rows] == ["E1"]


def test_one_building(auth_client, make_user, site):  # noqa: F811
    """`building`: that building's developers' money and the sales at its counters."""
    client = auth_client(make_user(Roles.FINANCE_MANAGER))
    body = client.get(f"{FINANCE_STATS}?building={site.b1.pk}").json()
    assert [b["code"] for b in body["buildings"]] == ["B1"]
    assert [r["employee_number"] for r in body["developers"]] == ["E1"]
    assert body == auth_client(site.owner).get(FINANCE_STATS).json()  # = B1's owner's view
    # A building owner can't look at another building.
    other = auth_client(site.owner).get(f"{FINANCE_STATS}?building={site.b2.pk}").json()
    assert other["developers"] == [] and other["money"]["deposits"]["total"] == "0.00"
    # The Excel download follows the filter.
    admin = auth_client(make_user(Roles.ADMIN))
    r = admin.get(f"/api/v1/exports/finance-stats/?building={site.b1.pk}")
    assert r.status_code == 200 and r["Content-Type"].startswith("application/vnd.openxml")


def test_by_building(auth_client, make_user, site):  # noqa: F811
    """All buildings: a row per building; it adds up to the totals."""
    from decimal import Decimal

    body = auth_client(make_user(Roles.FINANCE_MANAGER)).get(FINANCE_STATS).json()
    rows = {row["code"]: row for row in body["by_building"]}
    assert set(rows) >= {"B1", "B2"}
    total = sum(Decimal(row["deposits"]) for row in rows.values())
    assert total == Decimal(body["money"]["deposits"]["total"])
    held = sum(Decimal(row["balance"]) for row in rows.values())
    assert held == Decimal(body["money"]["developer_accounts"]["total_balance"])
    b1 = rows["B1"]
    assert Decimal(b1["net"]) == Decimal(b1["deposits"]) - Decimal(b1["spending"])
    # Each day, per building: they add up to the building's totals and the day's figures.
    for row in rows.values():
        assert sum(Decimal(d["deposits"]) for d in row["daily"]) == Decimal(row["deposits"])
        assert sum(Decimal(d["spending"]) for d in row["daily"]) == Decimal(row["spending"])
    for i, day in enumerate(body["money"]["daily"]):
        assert sum(Decimal(r["daily"][i]["deposits"]) for r in rows.values()) == Decimal(
            day["deposits"]
        )
    # One building chosen (or a one-building owner): no breakdown.
    one = auth_client(make_user(Roles.FINANCE_MANAGER)).get(
        f"{FINANCE_STATS}?building={site.b1.pk}"
    )
    assert one.json()["by_building"] == []
    assert auth_client(site.owner).get(FINANCE_STATS).json()["by_building"] == []


def test_seller_sales_by_building(auth_client, make_user, site):  # noqa: F811
    """Each seller's sales split by the buyer's building (all of them are Ada's: B1)."""
    from decimal import Decimal

    body = auth_client(make_user(Roles.FINANCE_MANAGER)).get(FINANCE_STATS).json()
    cafe = by_name(body)["Cafe"]
    parts = {part["code"]: part for part in cafe["by_building"]}
    assert parts["B1"]["total"] == "50.00" and set(parts) == {"B1"}
    assert sum(Decimal(p["total"]) for p in parts.values()) == Decimal(
        cafe["sales_total"]
    ) + Decimal(cafe["bookings_total"])
    # A building owner only sees their building's part.
    owner = by_name(auth_client(site.owner).get(FINANCE_STATS).json())["Cafe"]
    assert [p["code"] for p in owner["by_building"]] == ["B1"]


def test_a_building_whose_people_bought_nothing(auth_client, make_user, site):  # noqa: F811
    """Building 2 has a Cafe counter, but its people (Bob) bought nothing: no sellers."""
    body = (
        auth_client(make_user(Roles.FINANCE_MANAGER))
        .get(f"{FINANCE_STATS}?building={site.b2.pk}")
        .json()
    )
    assert body["sellers"] == []
    assert body["money"]["store_sales"]["sales_total"] == "0.00"
