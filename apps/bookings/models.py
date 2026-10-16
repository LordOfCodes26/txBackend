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

    Prepared at the playground desk as a line of a draft `Purchase` (rental good, start,
    number of slots); the developer taps their card and enters their PIN, and confirming
    the purchase creates the booking. So the ledger, the seller's earnings and the
    reconciliation checks treat it like any other sale. Until it starts, a booking can be
    moved to another time or court of the same price, or deleted with its purchase (the
    money goes back to the wallet; purchases.services.refund_purchase). Courts with
    `refund_after_start` also allow that deletion later, until the developer checks in.

    Exclusion constraints make overlapping bookings impossible at the database level, even
    under concurrent requests: of the same court, and by the same developer (one court at a
    time per person). Ranges are half-open
    [start, end), so back-to-back slots (13:00-14:00, 14:00-15:00) are fine.
    """

    good = models.ForeignKey(Good, on_delete=models.PROTECT, related_name="bookings")
    developer = models.ForeignKey(Developer, on_delete=models.PROTECT, related_name="bookings")
    purchase = models.ForeignKey(Purchase, on_delete=models.PROTECT, related_name="bookings")
    start = models.DateTimeField()
    end = models.DateTimeField()
    slots = models.PositiveSmallIntegerField()
    # How often the booking was moved to another time or court (see services.change_booking).
    change_count = models.PositiveSmallIntegerField(default=0)
    # Checked in at the playground: the developer tapped their card on a check reader.
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_in_at_store = models.ForeignKey(
        "sellers.Seller",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="The store (e.g. the gym counter) whose till reader took the tap.",
    )

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


class CheckResult(models.TextChoices):
    CHECKED_IN = "CHECKED_IN", "Checked in"
    ALREADY_CHECKED_IN = "ALREADY_CHECKED_IN", "Already checked in"
    NO_BOOKING = "NO_BOOKING", "No booking now"
    CARD_REFUSED = "CARD_REFUSED", "Card not accepted"


class PlaygroundCheck(models.Model):
    """One card tap on the till reader of a store that checks bookings, and what it found."""

    event = models.OneToOneField(
        "rfid.RFIDEvent", on_delete=models.CASCADE, related_name="playground_check"
    )
    reader = models.ForeignKey("rfid.RFIDDevice", on_delete=models.CASCADE, related_name="+")
    seller = models.ForeignKey(
        "sellers.Seller", on_delete=models.PROTECT, related_name="playground_checks"
    )
    # A log of taps: it goes with the developer or booking when they are deleted for good.
    developer = models.ForeignKey(
        Developer, on_delete=models.CASCADE, null=True, blank=True, related_name="+"
    )
    booking = models.ForeignKey(
        Booking, on_delete=models.CASCADE, null=True, blank=True, related_name="checks"
    )
    result = models.CharField(max_length=20, choices=CheckResult.choices)
    # CARD_REFUSED: the scan's own result (UNKNOWN_CARD, BLOCKED_CARD, ...).
    scan_result = models.CharField(max_length=30, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"{self.reader} {self.result} ({self.developer})"
