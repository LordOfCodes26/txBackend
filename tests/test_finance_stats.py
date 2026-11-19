"""Finance statistics (/stats/finance/): money and a per-seller comparison."""

from decimal import Decimal

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
    assert parts["B1"]["name"] == "Building 1"
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


def test_wallets_in_minus_by_building(auth_client, make_user, site):  # noqa: F811
    """What wallets below zero owe now: in total, and per home building (adding up)."""
    from decimal import Decimal

    from apps.developers.models import Developer
    from apps.finance import services as finance
    from apps.finance.models import DeveloperAccount

    carl = Developer.objects.create(employee_number="E3", full_name="Carl", building=site.b1)
    finance.open_account(carl)
    DeveloperAccount.objects.filter(developer=site.ada).update(balance=Decimal("-12.50"))
    DeveloperAccount.objects.filter(developer=carl).update(balance=Decimal("-3.00"))
    DeveloperAccount.objects.filter(developer=site.bob).update(balance=Decimal("-5.00"))
    dina = Developer.objects.create(employee_number="E4", full_name="Dina", building=site.b1)
    finance.deposit(actor=None, developer=dina, amount="40.00", idempotency_key="minus-dina")

    client = auth_client(make_user(Roles.FINANCE_MANAGER))
    body = client.get(FINANCE_STATS).json()
    assert body["money"]["minus"] == {"total": "-20.50", "count": 3}
    rows = {row["code"]: row for row in body["by_building"]}
    assert (rows["B1"]["minus"], rows["B1"]["minus_count"]) == ("-15.50", 2)
    assert (rows["B2"]["minus"], rows["B2"]["minus_count"]) == ("-5.00", 1)
    # Money held is the positive balances only: B1 holds Dina's 40.00, B2 nothing.
    assert (rows["B1"]["balance"], rows["B2"]["balance"]) == ("40.00", "0.00")
    held = sum(Decimal(r["balance"]) for r in rows.values())
    assert held == Decimal(body["money"]["developer_accounts"]["total_balance"])
    assert body["money"]["developer_accounts"]["total_balance"] == "40.00"
    one = client.get(f"{FINANCE_STATS}?building={site.b1.pk}").json()
    assert one["money"]["minus"] == {"total": "-15.50", "count": 2}


def test_deposits_by_type(auth_client, make_user, site):  # noqa: F811
    """The deposits split by type (E-commerce, Cash, none), and every deposit figure for
    one type with ?deposit_type=."""
    from apps.finance import services as finance

    finance.deposit(
        actor=None, developer=site.ada, amount="7.00", idempotency_key="e-1", method="ECOMMERCE"
    )
    finance.deposit(
        actor=None, developer=site.ada, amount="3.00", idempotency_key="c-1", method="CASH"
    )
    client = auth_client(make_user(Roles.FINANCE_MANAGER))
    deposits = client.get(FINANCE_STATS).json()["money"]["deposits"]
    assert deposits["total"] == "410.00"  # the 400.00 of the site has no type
    assert {k: (v["total"], v["count"]) for k, v in deposits["by_type"].items()} == {
        "ECOMMERCE": ("7.00", 1),
        "CASH": ("3.00", 1),
        "NONE": ("400.00", 2),
    }
    body = client.get(FINANCE_STATS, {"deposit_type": "ECOMMERCE"}).json()
    assert (body["money"]["deposits"]["total"], body["money"]["deposits"]["count"]) == ("7.00", 1)
    assert [
        (r["employee_number"], r["deposits"]) for r in body["developers"] if r["deposit_count"]
    ] == [("E1", "7.00")]
    assert client.get(FINANCE_STATS, {"deposit_type": "X"}).status_code == 400


def test_reversed_deposit_leaves_the_deposit_figures(auth_client, make_user, site):  # noqa: F811
    """A deposit entered by mistake and reversed counts for nothing; the right one counts."""
    from apps.finance import services as finance

    client = auth_client(make_user(Roles.FINANCE_MANAGER))

    def figures():
        body = client.get(FINANCE_STATS).json()
        deposits = body["money"]["deposits"]
        [ada] = [r for r in body["developers"] if r["employee_number"] == "E1"]
        return (
            Decimal(deposits["total"]),
            deposits["count"],
            deposits["by_type"]["CASH"],
            Decimal(ada["deposits"]),
            ada["deposit_count"],
        )

    total, count, cash, ada_total, ada_count = figures()
    wrong, _ = finance.deposit(
        actor=None, developer=site.ada, amount="70.00", idempotency_key="w-1", method="CASH"
    )
    finance.reverse_deposit(actor=None, deposit=wrong, reason="Typo", idempotency_key="r-1")
    finance.deposit(
        actor=None, developer=site.ada, amount="7.00", idempotency_key="ok-1", method="CASH"
    )
    assert cash == {"total": "0.00", "count": 0}
    assert figures() == (
        total + Decimal("7.00"),
        count + 1,
        {"total": "7.00", "count": 1},
        ada_total + Decimal("7.00"),
        ada_count + 1,
    )


def test_a_fix_counts_on_the_wrong_deposits_date(auth_client, make_user, site):  # noqa: F811
    """A deposit corrected (or reversed) a month later: the fix counts in the month of the
    wrong deposit, and leaves the month it was made in alone."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.finance import services as finance
    from apps.finance.models import AccountTransaction, DeveloperAccount

    now = timezone.localtime()
    then = now - timedelta(days=40)
    account = DeveloperAccount.objects.get(developer=site.ada)
    DeveloperAccount.objects.filter(pk=account.pk).update(balance=account.balance + 100)
    wrong = AccountTransaction.objects.create(
        account=account,
        kind="DEPOSIT",
        amount=100,
        balance_after=account.balance + 100,
        method="CASH",
        created_at=then,
    )
    client = auth_client(make_user(Roles.FINANCE_MANAGER))

    def cash(day):
        query = {"date_from": day.date().isoformat(), "date_to": day.date().isoformat()}
        return client.get(FINANCE_STATS, query).json()["money"]["deposits"]["by_type"]["CASH"]

    assert (cash(then), cash(now)) == (
        {"total": "100.00", "count": 1},
        {"total": "0.00", "count": 0},
    )
    right, _ = finance.correct_deposit(
        actor=None,
        deposit=wrong,
        amount="10.00",
        method="CASH",
        reason="Typo",
        idempotency_key="c-1",
    )
    assert right.counts_on == wrong.created_at
    assert (cash(then), cash(now)) == (
        {"total": "10.00", "count": 1},
        {"total": "0.00", "count": 0},
    )
    # The deposit list finds them on that day too.
    listed = client.get(
        "/api/v1/finance/transactions/deposits/",
        {
            "counted_after": then.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
            "counted_before": (then + timedelta(days=1)).replace(hour=0, minute=0).isoformat(),
        },
    ).json()["results"]
    assert sorted(
        row["amount"] for row in listed if row["developer"]["employee_number"] == "E1"
    ) == [
        "-100.00",
        "10.00",
        "100.00",
    ]
