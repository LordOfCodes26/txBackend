from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
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
    unit_price = serializers.SerializerMethodField()
    line_total = serializers.SerializerMethodField()

    class Meta:
        model = PurchaseItem
        fields = ["id", "good", "good_name", "quantity", "unit_price", "line_total"]
        read_only_fields = fields

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
        if own is not None and position.seller_id != own.pk:
            raise serializers.ValidationError("You can only sell at your own service positions.")
        return position


class SetReaderSerializer(serializers.Serializer):
    reader = ReaderField(allow_null=True)


class TillReaderSerializer(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()
    online = serializers.BooleanField(source="is_online")


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
