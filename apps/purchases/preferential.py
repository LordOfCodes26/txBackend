"""Preferential service: free items for developers who use the wallet a lot.

Usage is what a developer spent in a calendar month (company time zone): purchases minus
refunds, from the ledger. Deposits and manual adjustments don't count. Each month, the
previous month's usage earns the items of the highest level (rule) it reached, under the
settings in force for that month (`PreferentialRule`). The items are
one total, spent on any preferential good: one item per free unit (on a court, per slot).
Unused items carry over: what is left is everything earned minus everything used. Checkout
takes them automatically (`free_units`), the dearest units first; used items never come
back, even when the purchase is changed or deleted.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from apps.audit.services import record_audit
from apps.finance import services as finance
from apps.finance.models import AccountTransaction, TransactionKind

from .models import (
    PreferentialGood,
    PreferentialRule,
    PreferentialSettings,
    PurchaseItem,
    PurchaseStatus,
)

USAGE = Q(kind__in=(TransactionKind.PURCHASE, TransactionKind.REFUND))
# Free units stay used when the purchase is deleted later.
USED = (PurchaseStatus.CONFIRMED, PurchaseStatus.REFUNDED)


def _month_start(day: date) -> datetime:
    return timezone.make_aware(
        datetime.combine(day.replace(day=1), time.min), timezone.get_current_timezone()
    )


def _next_month(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def _previous_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def month_bounds(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    """(start of last month, start of this month, start of next month), local time."""
    this_month = timezone.localdate(now).replace(day=1)
    return (
        _month_start(_previous_month(this_month)),
        _month_start(this_month),
        _month_start(_next_month(this_month)),
    )


def _usage_by_month(developer_id: int, since: date, until: datetime) -> dict[date, Decimal]:
    return _usage_by_developer_month([developer_id], since, until).get(developer_id, {})


def _usage_by_developer_month(
    developer_ids, since: date, until: datetime
) -> dict[int, dict[date, Decimal]]:
    """Usage per developer and month (one query for many developers)."""
    rows = (
        AccountTransaction.objects.filter(
            USAGE,
            account__developer_id__in=developer_ids,
            created_at__gte=_month_start(since),
            created_at__lt=until,
        )
        .annotate(month=TruncMonth("created_at", tzinfo=timezone.get_current_timezone()))
        .order_by()
        .values("account__developer_id", "month")
        .annotate(t=Sum("amount"))
    )
    usage: dict[int, dict[date, Decimal]] = {}
    for row in rows:
        month = timezone.localdate(row["month"])
        usage.setdefault(row["account__developer_id"], {})[month] = finance.money(-row["t"])
    return usage


def usage_last_month(developer_id: int, now: datetime | None = None) -> Decimal:
    """Purchases minus refunds of the developer's wallet last calendar month."""
    start, end, _next = month_bounds(now)
    usage = _usage_by_month(developer_id, timezone.localdate(start), end)
    return usage.get(timezone.localdate(start), finance.money(0))


def items_for(levels: list[dict], usage: Decimal) -> int:
    """The items of the highest level `usage` reached (0: none)."""
    reached = [level["items"] for level in levels if usage >= Decimal(level["threshold"])]
    return max(reached, default=0)


def _rule_for(rules: list[PreferentialRule], month: date) -> PreferentialRule | None:
    """The settings in force for `month`: the last ones saved before it ended."""
    end = _month_start(_next_month(month))
    found = None
    for rule in rules:
        if rule.created_at >= end:
            break
        found = rule
    return found


def earned(developer_id: int, now: datetime | None = None) -> int:
    """Free items earned so far: every month from the first saved settings to this one."""
    return earned_many([developer_id], now).get(developer_id, 0)


def earned_many(developer_ids, now: datetime | None = None) -> dict[int, int]:
    """`earned` for many developers at once (a list page): three queries in all."""
    rules = list(PreferentialRule.objects.all())
    if not rules:
        return {}
    this_month = timezone.localdate(now).replace(day=1)
    first = timezone.localdate(rules[0].created_at).replace(day=1)
    usage = _usage_by_developer_month(
        developer_ids, _previous_month(first), _month_start(this_month)
    )
    return {
        developer_id: _earned(rules, usage.get(developer_id, {}), first, this_month)
        for developer_id in developer_ids
    }


def _earned(rules, usage: dict[date, Decimal], first: date, this_month: date) -> int:
    total = 0
    month = first
    while month <= this_month:
        rule = _rule_for(rules, month)
        spent = usage.get(_previous_month(month), Decimal("0.00"))
        if rule is not None and rule.enabled:
            total += items_for(rule.levels, spent)
        month = _next_month(month)
    return total


def used(developer_id: int) -> int:
    """Free items the developer took so far."""
    return used_many([developer_id]).get(developer_id, 0)


def used_many(developer_ids) -> dict[int, int]:
    rows = (
        PurchaseItem.objects.filter(
            purchase__developer_id__in=developer_ids,
            purchase__status__in=USED,
            free_quantity__gt=0,
        )
        .order_by()
        .values("purchase__developer_id")
        .annotate(n=Sum("free_quantity"))
    )
    return {row["purchase__developer_id"]: row["n"] for row in rows}


def is_eligible(developer_id: int, now: datetime | None = None) -> bool:
    """Earns free items this month (last month's usage reached a level)."""
    settings = PreferentialSettings.current()
    return settings.enabled and items_for(settings.levels, usage_last_month(developer_id, now)) > 0


def free_left(developer_id: int, now: datetime | None = None) -> int:
    """Free items the developer may still take (0 while the service is off)."""
    if not PreferentialSettings.current().enabled:
        return 0
    return max(earned(developer_id, now) - used(developer_id), 0)


def free_left_many(developer_ids, now: datetime | None = None) -> dict[int, int] | None:
    """`free_left` for many developers at once; None while the service is off."""
    if not PreferentialSettings.current().enabled:
        return None
    got, taken = earned_many(developer_ids, now), used_many(developer_ids)
    return {d: max(got.get(d, 0) - taken.get(d, 0), 0) for d in developer_ids}


def free_units(developer_id: int, items, now: datetime | None = None) -> dict[int, int]:
    """How many units of each line (by good) a purchase gets free: the developer's items
    go to the preferential goods' units, the dearest unit first. At checkout this runs with
    the developer's account locked, so two tills can't hand out the same items."""
    preferential = set(
        PreferentialGood.objects.filter(good_id__in=[i.good_id for i in items]).values_list(
            "good_id", flat=True
        )
    )
    lines = [item for item in items if item.good_id in preferential]
    if not lines:
        return {}
    left = free_left(developer_id, now)
    free = {}
    for item in sorted(lines, key=lambda i: (-_unit_value(i), i.good_id)):
        if left <= 0:
            break
        # Whole units only: 2.5 kg with 3 items left takes 2.
        taken = min(int(item.quantity), left)
        if taken:
            free[item.good_id] = taken
            left -= taken
    return free


def _unit_value(item) -> Decimal:
    """What one unit of the line costs (a court slot: for all its people)."""
    price = item.court_price.price if item.court_price_id else item.good.price
    return price * item.people


def charged(item) -> Decimal:
    """Units of a line that cost money."""
    return max(item.quantity - item.free_quantity, 0)


def developer_status(developer_id: int, now: datetime | None = None) -> dict:
    """For the developer's page: last month's usage, whether it earned this month's free
    items, and the items earned, used and left (saved up)."""
    settings = PreferentialSettings.current()
    usage = usage_last_month(developer_id, now)
    got, taken = earned(developer_id, now), used(developer_id)
    this_month = items_for(settings.levels, usage) if settings.enabled else 0
    return {
        "enabled": settings.enabled,
        "levels": settings.levels,
        "usage_last_month": f"{usage:.2f}",
        "eligible": this_month > 0,
        "items_this_month": this_month,
        "earned": got,
        "used": taken,
        "left": max(got - taken, 0) if settings.enabled else 0,
    }


def report(first: date, last: date) -> list[dict]:
    """Free units given from `first` to `last` (company days), per developer and good:
    the units and their value at the price charged that day. Deleted purchases count too,
    as their free units stay used. Biggest value first."""
    start = timezone.make_aware(datetime.combine(first, time.min), timezone.get_current_timezone())
    end = timezone.make_aware(
        datetime.combine(last + timedelta(days=1), time.min), timezone.get_current_timezone()
    )
    items = (
        PurchaseItem.objects.filter(
            free_quantity__gt=0,
            purchase__status__in=USED,
            purchase__confirmed_at__gte=start,
            purchase__confirmed_at__lt=end,
        )
        .select_related("purchase__developer", "good__service_position__seller")
        .order_by("purchase__confirmed_at", "id")
    )
    rows: dict[tuple[int, int], dict] = {}
    for item in items:
        developer, good = item.purchase.developer, item.good
        row = rows.setdefault(
            (developer.pk, good.pk),
            {
                "developer": developer.pk,
                "employee_number": developer.employee_number,
                "full_name": developer.full_name,
                "good": good.pk,
                "good_name": good.name,
                "seller_name": good.service_position.seller.name,
                "units": 0,
                "value": Decimal("0.00"),
                "last_at": None,
            },
        )
        # A court line's unit is a slot (for all its people).
        row["units"] += item.free_quantity
        row["value"] += finance.money(item.unit_price * item.free_quantity * item.people)
        row["last_at"] = item.purchase.confirmed_at
    return sorted(rows.values(), key=lambda r: (-r["value"], r["full_name"], r["good_name"]))


@transaction.atomic
def save_settings(*, actor, enabled: bool, levels: list[dict], goods: list) -> PreferentialSettings:
    """Set the service: on/off, the levels ({"threshold", "items"}: usage last month ->
    free items this month), and the whole list of preferential goods (a good left out is
    no longer one). Kept as a new
    `PreferentialRule`, so months already over keep what they earned."""
    current = PreferentialSettings.current()
    current = PreferentialSettings.objects.select_for_update().get(pk=current.pk)
    old = {
        "enabled": current.enabled,
        "levels": current.levels,
        "goods": sorted(PreferentialGood.objects.values_list("good_id", flat=True)),
    }
    levels = sorted(
        (
            {"threshold": f"{finance.money(level['threshold']):.2f}", "items": int(level["items"])}
            for level in levels
        ),
        key=lambda level: Decimal(level["threshold"]),
    )
    current.enabled = enabled
    current.levels = levels
    current.updated_by = actor if getattr(actor, "pk", None) else None
    current.save(update_fields=["enabled", "levels", "updated_by", "updated_at"])

    wanted = sorted({good.pk for good in goods})
    PreferentialGood.objects.exclude(good_id__in=wanted).delete()
    have = set(PreferentialGood.objects.values_list("good_id", flat=True))
    PreferentialGood.objects.bulk_create(
        [PreferentialGood(good_id=good_id) for good_id in wanted if good_id not in have]
    )
    PreferentialRule.objects.create(enabled=current.enabled, levels=levels, goods=wanted)

    record_audit(
        "purchase.preferential_changed",
        actor=actor,
        entity=current,
        old_values=old,
        new_values={
            "enabled": current.enabled,
            "levels": levels,
            "goods": wanted,
        },
    )
    return current
