from django.conf import settings
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


class BookingStatus(models.TextChoices):
    RESERVED = "RESERVED", "Reserved (not paid yet)"
    PAID = "PAID", "Paid"


class Booking(TimeStampedModel):
    """An exclusive, prepaid time slot on a RENTAL good (playground, pool, ...).

    Prepared at the playground desk as a line of a draft `Purchase` (rental good, start,
    number of slots); the developer taps their card and enters their PIN, and confirming
    the purchase creates the booking. So the ledger, the seller's earnings and the
    reconciliation checks treat it like any other sale. Until it starts, a booking can be
    moved to another time or court of the same price, or deleted with its purchase (the
    money goes back to the wallet; purchases.services.refund_purchase). Courts with
    `refund_after_start` also allow that later.

    Exclusion constraints make overlapping bookings impossible at the database level, even
    under concurrent requests: of the same court, and by the same developer (one court at a
    time per person). Ranges are half-open
    [start, end), so back-to-back slots (13:00-14:00, 14:00-15:00) are fine.
    """

    good = models.ForeignKey(Good, on_delete=models.PROTECT, related_name="bookings")
    developer = models.ForeignKey(Developer, on_delete=models.PROTECT, related_name="bookings")
    # RESERVED: booked for free, paid on arrival (then PAID, linked to its purchase). PAID
    # straight away: "play now", or a booking paid when it was made.
    status = models.CharField(
        max_length=10, choices=BookingStatus.choices, default=BookingStatus.PAID
    )
    purchase = models.ForeignKey(
        Purchase, on_delete=models.PROTECT, related_name="bookings", null=True, blank=True
    )
    # Which of the court's prices (None: older bookings, the court's own price).
    court_price = models.ForeignKey(
        "goods.CourtPrice", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    # Per person prices: how many play (one card pays for all); 1 on per court ones.
    people = models.PositiveSmallIntegerField(default=1)
    reserved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    start = models.DateTimeField()
    end = models.DateTimeField()
    slots = models.PositiveSmallIntegerField()
    # How often the booking was moved to another time or court (see services.change_booking).
    change_count = models.PositiveSmallIntegerField(default=0)

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
