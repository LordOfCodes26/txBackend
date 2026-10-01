from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.developers.serializers import DeveloperSummarySerializer
from apps.goods.models import Good
from apps.rfid.serializers import UIDField
from apps.sellers.models import ServicePosition
from apps.sellers.serializers import SellerSummarySerializer

from .models import Purchase, PurchaseItem


class PurchaseItemSerializer(serializers.ModelSerializer):
    good_name = serializers.CharField(source="good.name", read_only=True)
    kind = serializers.CharField(source="good.kind", read_only=True)
    start = serializers.DateTimeField(
        read_only=True, help_text="Bookings: start of the first slot; null for goods."
    )
    end = serializers.SerializerMethodField(help_text="Bookings: end of the last slot.")
    date = serializers.SerializerMethodField(help_text="Bookings: company-local day.")
    start_time = serializers.SerializerMethodField(help_text="Bookings: local start, HH:MM.")
    end_time = serializers.SerializerMethodField(help_text="Bookings: local end, HH:MM.")
    unit_price = serializers.SerializerMethodField()
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseItem
        fields = [
            "id",
            "good",
            "good_name",
            "kind",
            "quantity",
            "date",
            "start_time",
            "end_time",
            "start",
            "end",
            "unit_price",
            "line_total",
        ]
        read_only_fields = fields

    @staticmethod
    def _end(item):
        rental = getattr(item.good, "rental", None)
        if item.start is None or rental is None:
            return None
        return item.start + timedelta(minutes=rental.slot_minutes * item.quantity)

    def get_end(self, item) -> str | None:
        end = self._end(item)
        return end.isoformat() if end else None

    def get_date(self, item) -> str | None:
        return timezone.localtime(item.start).date().isoformat() if item.start else None

    def get_start_time(self, item) -> str | None:
        return f"{timezone.localtime(item.start):%H:%M}" if item.start else None

    def get_end_time(self, item) -> str | None:
        end = self._end(item)
        return f"{timezone.localtime(end):%H:%M}" if end else None

    # Drafts show the current price; confirmed purchases show what was charged.
    def get_unit_price(self, item) -> str:
        price = item.unit_price if item.unit_price is not None else item.good.price
        return f"{price:.2f}"

    def get_line_total(self, item) -> str:
        if item.line_total is not None:
            return f"{item.line_total:.2f}"
        return f"{item.good.price * item.quantity:.2f}"


class PresentedCardSerializer(serializers.Serializer):
    """Who tapped on the counter's reader, for the seller's screen."""

    developer = DeveloperSummarySerializer(source="presented_event.developer")
    presented_at = serializers.DateTimeField()
    expires_at = serializers.SerializerMethodField()

    def get_expires_at(self, purchase) -> str:
        window = timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS)
        return (purchase.presented_at + window).isoformat()


class PurchaseSerializer(serializers.ModelSerializer):
    seller = SellerSummarySerializer(read_only=True)
    service_position_name = serializers.CharField(source="service_position.name", read_only=True)
    reader = serializers.SlugRelatedField(slug_field="code", read_only=True)
    developer = DeveloperSummarySerializer(read_only=True)
    card_uid = serializers.CharField(source="card.uid", read_only=True, default=None)
    items = PurchaseItemSerializer(many=True, read_only=True)
    presented_card = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    balance_after = serializers.DecimalField(
        source="account_transaction.balance_after",
        max_digits=14,
        decimal_places=2,
        read_only=True,
        default=None,
    )

    class Meta:
        model = Purchase
        fields = [
            "id",
            "kind",
            "status",
            "seller",
            "service_position",
            "service_position_name",
            "reader",
            "items",
            "total",
            "currency",
            "presented_card",
            "developer",
            "card_uid",
            "balance_after",
            "created_by",
            "created_at",
            "confirmed_by",
            "confirmed_at",
            "cancelled_at",
        ]
        read_only_fields = fields

    def get_presented_card(self, purchase) -> dict | None:
        """The card tapped on this counter's reader, while still valid (drafts only)."""
        if purchase.status != "DRAFT" or purchase.presented_event_id is None:
            return None
        window = timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS)
        if purchase.presented_at < timezone.now() - window:
            return None
        return PresentedCardSerializer(purchase).data

    def get_total(self, purchase) -> str:
        if purchase.total is not None:
            return f"{purchase.total:.2f}"
        total = sum((i.good.price * i.quantity for i in purchase.items.all()), Decimal("0.00"))
        return f"{total:.2f}"

    def get_currency(self, obj) -> str:
        return settings.CURRENCY


def _readers():
    from apps.rfid.models import DevicePurpose, RFIDDevice

    return RFIDDevice.objects.filter(purpose=DevicePurpose.TILL, is_active=True)


@extend_schema_field(OpenApiTypes.STR)
class ReaderField(serializers.SlugRelatedField):
    """A till reader by its code, e.g. "Reader2"."""

    def __init__(self, **kwargs):
        super().__init__(slug_field="code", **kwargs)

    def get_queryset(self):
        return _readers()


class PurchaseCreateSerializer(serializers.Serializer):
    service_position = serializers.PrimaryKeyRelatedField(
        queryset=ServicePosition.objects.select_related("seller")
    )
    reader = ReaderField(
        required=False,
        allow_null=True,
        help_text="Code of the till reader plugged into this PC (e.g. Reader2); taps on it "
        "pay this purchase.",
    )

    def validate_service_position(self, position):
        own = self.context.get("own_seller")
        positions = self.context.get("own_positions")
        if (own is not None and position.seller_id != own.pk) or (
            positions is not None and position.pk not in positions
        ):
            raise serializers.ValidationError(_("You can only sell at your own service positions."))
        return position


class SetReaderSerializer(serializers.Serializer):
    reader = ReaderField(allow_null=True)


class TillReaderSerializer(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()
    online = serializers.BooleanField(source="is_online")
    last_seen_at = serializers.DateTimeField()


class DetectedReaderSerializer(serializers.Serializer):
    ip = serializers.IPAddressField(allow_null=True)
    reader = TillReaderSerializer(allow_null=True)
    candidates = TillReaderSerializer(many=True)


class ItemAddSerializer(serializers.Serializer):
    good = serializers.PrimaryKeyRelatedField(queryset=Good.objects.all())
    quantity = serializers.IntegerField(min_value=1, max_value=999, default=1)


class ItemUpdateSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, max_value=999)


class ConfirmSerializer(serializers.Serializer):
    card_uid = UIDField(
        required=False,
        help_text="Leave out: the card tapped on the counter's reader is used. Typed UIDs are "
        "accepted only if the server allows manual entry.",
    )
    pin = serializers.CharField(
        write_only=True, max_length=6, help_text="PIN typed by the developer."
    )


class PerformanceSerializer(serializers.Serializer):
    service_position = serializers.IntegerField()
    service_position_name = serializers.CharField()
    seller = serializers.IntegerField()
    seller_name = serializers.CharField()
    building = serializers.IntegerField(allow_null=True)
    building_name = serializers.CharField(allow_null=True)
    sales_count = serializers.IntegerField(help_text="Confirmed till sales.")
    sales_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    bookings_count = serializers.IntegerField(help_text="Paid court bookings.")
    bookings_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    total = serializers.DecimalField(max_digits=14, decimal_places=2)
