from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from apps.developers.serializers import DeveloperSummarySerializer
from apps.goods.models import Good
from apps.goods.serializers import GoodImageSerializer, RentalSettingsSerializer
from apps.sellers.serializers import SellerSummarySerializer

from .models import Booking


class RentalSerializer(serializers.ModelSerializer):
    """A bookable rental as developers see it."""

    seller = SellerSummarySerializer(source="service_position.seller", read_only=True)
    location = serializers.CharField(source="service_position.location", read_only=True)
    rental = RentalSettingsSerializer(read_only=True)
    images = GoodImageSerializer(many=True, read_only=True)
    currency = serializers.SerializerMethodField()

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
            "images",
        ]
        read_only_fields = fields

    def get_currency(self, obj) -> str:
        return settings.CURRENCY


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


class ScheduleSerializer(serializers.Serializer):
    date = serializers.DateField()
    courts = CourtDaySerializer(many=True)


class BookingSerializer(serializers.ModelSerializer):
    good_name = serializers.CharField(source="good.name", read_only=True)
    seller = SellerSummarySerializer(source="good.service_position.seller", read_only=True)
    location = serializers.CharField(source="good.service_position.location", read_only=True)
    developer = DeveloperSummarySerializer(read_only=True)
    total = serializers.DecimalField(
        source="purchase.total", max_digits=14, decimal_places=2, read_only=True
    )
    balance_after = serializers.DecimalField(
        source="purchase.account_transaction.balance_after",
        max_digits=14,
        decimal_places=2,
        read_only=True,
    )
    currency = serializers.SerializerMethodField()
    date = serializers.SerializerMethodField(help_text="Company-local day.")
    start_time = serializers.SerializerMethodField(help_text="Local start, HH:MM.")
    end_time = serializers.SerializerMethodField(help_text="Local end, HH:MM.")

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
            "total",
            "currency",
            "balance_after",
            "purchase",
            "created_at",
        ]
        read_only_fields = fields

    def get_currency(self, obj) -> str:
        return settings.CURRENCY

    def get_date(self, obj) -> str:
        return timezone.localtime(obj.start).date().isoformat()

    def get_start_time(self, obj) -> str:
        return f"{timezone.localtime(obj.start):%H:%M}"

    def get_end_time(self, obj) -> str:
        return f"{timezone.localtime(obj.end):%H:%M}"
