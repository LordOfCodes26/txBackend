from django.conf import settings
from rest_framework import serializers

from apps.developers.serializers import DeveloperSummarySerializer
from apps.goods.models import Good, GoodKind
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


class SlotSerializer(serializers.Serializer):
    start = serializers.DateTimeField()
    end = serializers.DateTimeField()
    available = serializers.BooleanField()


class BookingCreateSerializer(serializers.Serializer):
    good = serializers.PrimaryKeyRelatedField(
        queryset=Good.objects.filter(kind=GoodKind.RENTAL), help_text="The rental to book."
    )
    start = serializers.DateTimeField(help_text="Start of the first slot (from availability).")
    slots = serializers.IntegerField(min_value=1, default=1)
    pin = serializers.CharField(write_only=True, max_length=6)


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

    class Meta:
        model = Booking
        fields = [
            "id",
            "good",
            "good_name",
            "seller",
            "location",
            "developer",
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
