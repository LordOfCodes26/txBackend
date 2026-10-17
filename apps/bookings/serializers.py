from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.developers.models import Developer
from apps.developers.serializers import DeveloperSummarySerializer
from apps.finance.models import PIN_LENGTH
from apps.goods.models import Good, GoodKind
from apps.goods.serializers import (
    CourtPriceSerializer,
    GoodImageSerializer,
    RentalSettingsSerializer,
)
from apps.purchases.serializers import ReaderField, TillReaderSerializer
from apps.rfid.models import RFIDEvent
from apps.sellers.serializers import SellerSummarySerializer

from .models import Booking, PlaygroundCheck


class RentalSerializer(serializers.ModelSerializer):
    """A bookable rental as developers see it."""

    seller = SellerSummarySerializer(source="service_position.seller", read_only=True)
    location = serializers.CharField(source="service_position.location", read_only=True)
    rental = RentalSettingsSerializer(read_only=True)
    images = GoodImageSerializer(many=True, read_only=True)
    currency = serializers.SerializerMethodField()
    prices = serializers.SerializerMethodField()

    class Meta:
        model = Good
        fields = [
            "id",
            "name",
            "description",
            "price",
            "currency",
            "seller",
            "location",
            "rental",
            "prices",
            "images",
        ]
        read_only_fields = fields

    def get_currency(self, obj) -> str:
        return settings.CURRENCY

    @extend_schema_field(CourtPriceSerializer(many=True))
    def get_prices(self, obj) -> list:
        """The court's prices the desk can pick from."""
        rows = [p for p in obj.court_prices.all() if p.is_active]
        return CourtPriceSerializer(rows, many=True).data


class AvailabilityQuerySerializer(serializers.Serializer):
    date = serializers.DateField(help_text="Company-local date, YYYY-MM-DD.")


SLOT_STATES = ["FREE", "BOOKED", "PAST", "NOT_YET_OPEN"]


class LocalTimesMixin(serializers.Serializer):
    """`start` / `end` as full timestamps plus company-local `start_time` / `end_time`."""

    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    start_time = serializers.SerializerMethodField(help_text="Local, HH:MM.")
    end_time = serializers.SerializerMethodField(help_text="Local, HH:MM.")

    def get_start_time(self, obj) -> str:
        return f"{timezone.localtime(obj['start']):%H:%M}"

    def get_end_time(self, obj) -> str:
        return f"{timezone.localtime(obj['end']):%H:%M}"


class SlotSerializer(LocalTimesMixin):
    available = serializers.BooleanField()
    state = serializers.ChoiceField(choices=SLOT_STATES)


class PeriodSerializer(LocalTimesMixin):
    state = serializers.ChoiceField(choices=SLOT_STATES)


class CourtDaySerializer(serializers.Serializer):
    good = serializers.IntegerField(source="good.pk")
    name = serializers.CharField(source="good.name")
    price = serializers.DecimalField(source="good.price", max_digits=12, decimal_places=2)
    open = serializers.BooleanField(help_text="False on days the court is closed.")
    opening_time = serializers.TimeField()
    closing_time = serializers.TimeField()
    slot_minutes = serializers.IntegerField()
    periods = PeriodSerializer(many=True)
    bookings = serializers.ListField(
        child=serializers.DictField(), help_text="The day's bookings: who, when, paid or not."
    )


class ScheduleSerializer(serializers.Serializer):
    date = serializers.DateField()
    courts = CourtDaySerializer(many=True)


class BookingSerializer(serializers.ModelSerializer):
    good_name = serializers.CharField(source="good.name", read_only=True)
    seller = SellerSummarySerializer(source="good.service_position.seller", read_only=True)
    location = serializers.CharField(source="good.service_position.location", read_only=True)
    developer = DeveloperSummarySerializer(read_only=True)
    total = serializers.DecimalField(
        source="purchase.total",
        max_digits=14,
        decimal_places=2,
        read_only=True,
        default=None,
        help_text="What was paid; null while the booking is reserved (not paid).",
    )
    price = serializers.SerializerMethodField(
        help_text="What it costs: the chosen price x slots (x people for a per person price)."
    )
    price_name = serializers.SerializerMethodField(help_text="Which of the court's prices.")
    place = serializers.CharField(
        source="good.rental.place",
        read_only=True,
        default="OUTDOOR",
        help_text="INDOOR (checked in at the indoor check) or OUTDOOR (at the desk).",
    )
    balance_after = serializers.DecimalField(
        source="purchase.account_transaction.balance_after",
        max_digits=14,
        decimal_places=2,
        read_only=True,
        default=None,
    )
    currency = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField(help_text="Company-local day.")
    start_time = serializers.SerializerMethodField(help_text="Local start, HH:MM.")
    end_time = serializers.SerializerMethodField(help_text="Local end, HH:MM.")
    checked_in_at_store_name = serializers.CharField(
        source="checked_in_at_store.name",
        read_only=True,
        default=None,
        help_text="The store whose till reader checked the developer in (e.g. the gym).",
    )

    class Meta:
        model = Booking
        fields = [
            "id",
            "good",
            "good_name",
            "seller",
            "location",
            "developer",
            "date",
            "start_time",
            "end_time",
            "start",
            "end",
            "slots",
            "status",
            "people",
            "price",
            "price_name",
            "place",
            "change_count",
            "checked_in_at",
            "checked_in_at_store_name",
            "total",
            "currency",
            "balance_after",
            "purchase",
            "created_at",
        ]
        read_only_fields = fields

    def get_currency(self, obj) -> str:
        return settings.CURRENCY

    def get_price(self, obj) -> str:
        from .services import price_of

        return f"{price_of(obj.good, obj.slots, obj.people, obj.court_price):.2f}"

    def get_price_name(self, obj) -> str:
        return obj.court_price.name if obj.court_price_id else ""

    def get_date(self, obj) -> str:
        return timezone.localtime(obj.start).date().isoformat()

    def get_start_time(self, obj) -> str:
        return f"{timezone.localtime(obj.start):%H:%M}"

    def get_end_time(self, obj) -> str:
        return f"{timezone.localtime(obj.end):%H:%M}"


def _own_court(context, good):
    own = context.get("own_seller")
    positions = context.get("own_positions")
    if (own is not None and good.service_position.seller_id != own.pk) or (
        positions is not None and good.service_position_id not in positions
    ):
        raise serializers.ValidationError(_("You can only book your own courts."))
    return good


class BookingTimeSerializer(serializers.Serializer):
    """Company-local date and start / end time: any start minute, whole slots long."""

    date = serializers.DateField(help_text="YYYY-MM-DD")
    start_time = serializers.TimeField(help_text="e.g. 10:00")
    end_time = serializers.TimeField(help_text="e.g. 12:00")

    def validate(self, attrs):
        # An end before the start runs past midnight (the next day); the same time is none.
        if attrs["end_time"] == attrs["start_time"]:
            raise serializers.ValidationError(
                {"end_time": [_("The end time must be after the start time.")]}
            )
        return attrs


class CheckoutCreateSerializer(serializers.Serializer):
    """One of: a day and time (`date`, `start_time`, `end_time`), "play now"
    (`now_slots`), or a reserved booking paid on arrival (`booking`)."""

    good = serializers.PrimaryKeyRelatedField(
        queryset=Good.objects.filter(kind=GoodKind.RENTAL),
        required=False,
        help_text="The court (not with `booking`).",
    )
    date = serializers.DateField(required=False, help_text="YYYY-MM-DD")
    start_time = serializers.TimeField(required=False, help_text="e.g. 10:00")
    end_time = serializers.TimeField(required=False, help_text="e.g. 12:00")
    now_slots = serializers.IntegerField(
        min_value=1,
        max_value=24,
        required=False,
        help_text='"Play now": slots from the one running now.',
    )
    booking = serializers.PrimaryKeyRelatedField(
        queryset=Booking.objects.all(), required=False, help_text="A reserved booking to pay."
    )
    people = serializers.IntegerField(
        min_value=1, default=1, help_text="Per person prices: how many people play."
    )
    price = serializers.IntegerField(
        required=False, help_text="Which of the court's prices (id). Default: its first."
    )
    reader = ReaderField(
        required=False,
        allow_null=True,
        help_text="Code of the reader the developer taps: a till reader of the court's store "
        "or a card assign (master) reader (see `readers`). Left out: the store's only till "
        "reader, if it has exactly one.",
    )
    event = serializers.PrimaryKeyRelatedField(
        queryset=RFIDEvent.objects.all(),
        required=False,
        help_text="A tap just made on the reader (e.g. on arrival): it counts as the card.",
    )

    def validate_good(self, good):
        return _own_court(self.context, good)

    def validate(self, attrs):
        if attrs.get("booking") is not None:
            return attrs
        if attrs.get("good") is None:
            raise serializers.ValidationError({"good": [_("This field is required.")]})
        if attrs.get("now_slots") is not None:
            return attrs
        missing = [k for k in ("date", "start_time", "end_time") if attrs.get(k) is None]
        if missing:
            raise serializers.ValidationError({k: [_("This field is required.")] for k in missing})
        # An end before the start runs past midnight (the next day); the same time is none.
        if attrs["end_time"] == attrs["start_time"]:
            raise serializers.ValidationError(
                {"end_time": [_("The end time must be after the start time.")]}
            )
        return attrs


class ReserveSerializer(BookingTimeSerializer):
    """Book a court for free; the developer pays on arrival."""

    good = serializers.PrimaryKeyRelatedField(
        queryset=Good.objects.filter(kind=GoodKind.RENTAL), help_text="The court."
    )
    developer = serializers.PrimaryKeyRelatedField(
        queryset=Developer.objects.all(), help_text="Who it is for."
    )
    people = serializers.IntegerField(min_value=1, default=1)
    price = serializers.IntegerField(
        required=False, help_text="Which of the court's prices (id). Default: its first."
    )

    def validate_good(self, good):
        return _own_court(self.context, good)


class PayBookingSerializer(serializers.Serializer):
    reader = ReaderField(required=False, allow_null=True)
    event = serializers.PrimaryKeyRelatedField(
        queryset=RFIDEvent.objects.all(),
        required=False,
        help_text="The arrival tap: it counts as the card.",
    )


class BookingChangeSerializer(BookingTimeSerializer):
    good = serializers.PrimaryKeyRelatedField(
        queryset=Good.objects.filter(kind=GoodKind.RENTAL),
        required=False,
        help_text="Another court of the same seller; leave out to keep the court.",
    )
    pin = serializers.CharField(
        write_only=True,
        max_length=PIN_LENGTH,
        required=False,
        allow_blank=True,
        help_text="The developer's PIN; needed only when the new time costs more.",
    )

    def validate_good(self, good):
        return _own_court(self.context, good)


class DeskReadersQuerySerializer(serializers.Serializer):
    good = serializers.PrimaryKeyRelatedField(
        queryset=Good.objects.filter(kind=GoodKind.RENTAL), help_text="The court."
    )


class DeskReaderSerializer(TillReaderSerializer):
    kind = serializers.CharField(
        source="purpose",
        help_text="TILL: the court's store's till reader; ENROLL: a card assign (master) reader.",
    )


class CheckedBookingSerializer(serializers.ModelSerializer):
    good_name = serializers.CharField(source="good.name", read_only=True)
    date = serializers.SerializerMethodField()
    start_time = serializers.SerializerMethodField()
    end_time = serializers.SerializerMethodField()

    class Meta:
        model = Booking
        fields = ["id", "good", "good_name", "date", "start_time", "end_time", "checked_in_at"]

    def get_date(self, obj) -> str:
        return timezone.localtime(obj.start).date().isoformat()

    def get_start_time(self, obj) -> str:
        return f"{timezone.localtime(obj.start):%H:%M}"

    def get_end_time(self, obj) -> str:
        return f"{timezone.localtime(obj.end):%H:%M}"


class PlaygroundCheckSerializer(serializers.ModelSerializer):
    developer = DeveloperSummarySerializer(read_only=True)
    booking = CheckedBookingSerializer(read_only=True)
    reader = serializers.CharField(source="reader.code", read_only=True)
    reader_name = serializers.CharField(source="reader.name", read_only=True)
    seller_name = serializers.CharField(source="seller.name", read_only=True)
    uid = serializers.CharField(source="event.uid", read_only=True)
    message = serializers.SerializerMethodField(help_text="What the reader's screen showed.")

    class Meta:
        model = PlaygroundCheck
        fields = [
            "id",
            "result",
            "scan_result",
            "message",
            "developer",
            "booking",
            "reader",
            "reader_name",
            "seller_name",
            "uid",
            "created_at",
        ]

    def get_message(self, obj) -> str:
        from apps.rfid.serializers import check_message

        return str(check_message(obj.event))


class PlaygroundChecksQuerySerializer(serializers.Serializer):
    after = serializers.IntegerField(
        required=False, min_value=0, help_text="Only checks newer than this id (to poll)."
    )
