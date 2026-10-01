from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.db import models
from django.db.models import F, Func, Q

from apps.developers.models import Developer
from apps.goods.models import Good
from apps.purchases.models import Purchase
from common.models import TimeStampedModel


class TsTzRange(Func):
    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Booking(TimeStampedModel):
    """An exclusive, prepaid time slot on a RENTAL good (playground, pool, ...).

    Paid through a CONFIRMED `Purchase`, so the developer's ledger, the seller's earnings
    and the reconciliation checks treat it like any other sale. Bookings are final.

    Exclusion constraints make overlapping bookings impossible at the database level, even
    under concurrent requests: of the same court, and by the same developer (one court at a
    time per person). Ranges are half-open
    [start, end), so back-to-back slots (13:00-14:00, 14:00-15:00) are fine.
    """

    good = models.ForeignKey(Good, on_delete=models.PROTECT, related_name="bookings")
    developer = models.ForeignKey(Developer, on_delete=models.PROTECT, related_name="bookings")
    purchase = models.OneToOneField(Purchase, on_delete=models.PROTECT, related_name="booking")
    start = models.DateTimeField()
    end = models.DateTimeField()
    slots = models.PositiveSmallIntegerField()

    class Meta:
        ordering = ["-start", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(end__gt=F("start")), name="booking_ends_after_start"
            ),
            ExclusionConstraint(
                name="booking_no_overlap",
                expressions=[
                    ("good", RangeOperators.EQUAL),
                    (TsTzRange("start", "end", RangeBoundary()), RangeOperators.OVERLAPS),
                ],
            ),
            # One developer can't hold two courts at the same time.
            ExclusionConstraint(
                name="booking_one_court_per_developer",
                expressions=[
                    ("developer", RangeOperators.EQUAL),
                    (TsTzRange("start", "end", RangeBoundary()), RangeOperators.OVERLAPS),
                ],
            ),
        ]
        indexes = [models.Index(fields=["developer", "start"])]

    def __str__(self):
        return f"{self.good} {self.start:%Y-%m-%d %H:%M} ({self.developer})"
