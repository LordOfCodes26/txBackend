from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.developers.serializers import DeveloperSummarySerializer
from apps.finance.models import PIN_LENGTH
from apps.goods.models import Good
from apps.rfid.serializers import UIDField
from apps.sellers.models import ServicePosition
from apps.sellers.serializers import SellerSummarySerializer

from .models import Purchase, PurchaseItem, PurchaseStatus

TAKES_CARD = (PurchaseStatus.DRAFT, PurchaseStatus.CONFIRMED)


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
    balance = serializers.SerializerMethodField(
        help_text="Their wallet balance now, so the seller can see whether it covers the total."
    )
    presented_at = serializers.DateTimeField()
    expires_at = serializers.SerializerMethodField()

    def get_balance(self, purchase) -> str | None:
        from apps.finance.models import DeveloperAccount

        balance = (
            DeveloperAccount.objects.filter(developer_id=purchase.presented_event.developer_id)
            .values_list("balance", flat=True)
            .first()
        )
        return None if balance is None else f"{balance:.2f}"

    def get_expires_at(self, purchase) -> str:
        window = timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS)
        return (purchase.presented_at + window).isoformat()


class PurchaseSerializer(serializers.ModelSerializer):
    seller = SellerSummarySerializer(read_only=True)
    service_position_name = serializers.CharField(source="service_position.name", read_only=True)
    reader = serializers.SlugRelatedField(slug_field="code", read_only=True)
    reader_name = serializers.SerializerMethodField(
        help_text="The reader's name (its code when it has none)."
    )
    developer = DeveloperSummarySerializer(read_only=True)
    card_uid = serializers.CharField(source="card.uid", read_only=True, default=None)
    items = PurchaseItemSerializer(many=True, read_only=True)
    booking = serializers.SerializerMethodField(
        help_text="Court bookings: the booking's id (to move it); null otherwise."
    )
    presented_card = serializers.SerializerMethodField()
    waiting_for_card = serializers.SerializerMethodField(
        help_text="The seller pressed Scan card to buy: the next tap on the reader goes here."
    )
    total = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()
    balance_after = serializers.SerializerMethodField(
        help_text="The wallet balance after the purchase's last money movement (the payment, "
        "or a later refund or extra charge)."
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
            "reader_name",
            "items",
            "booking",
            "total",
            "currency",
            "presented_card",
            "waiting_for_card",
            "developer",
            "card_uid",
            "balance_after",
            "created_by",
            "created_at",
            "confirmed_by",
            "confirmed_at",
            "cancelled_at",
            "refunded_by",
            "refunded_at",
        ]
        read_only_fields = fields

    def get_booking(self, purchase) -> int | None:
        if purchase.kind != "BOOKING" or purchase.status != "CONFIRMED":
            return None
        return purchase.bookings.values_list("pk", flat=True).first()

    def get_presented_card(self, purchase) -> dict | None:
        """The card tapped on this counter's reader, while still valid (to pay a draft, or a
        paid purchase's dearer change)."""
        if (
            purchase.status not in TAKES_CARD
            or purchase.presented_event_id is None
            or purchase.presented_at is None  # the tap was used
        ):
            return None
        window = timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS)
        if purchase.presented_at < timezone.now() - window:
            return None
        return PresentedCardSerializer(purchase).data

    def get_waiting_for_card(self, purchase) -> bool:
        if purchase.status not in TAKES_CARD or purchase.waiting_since is None:
            return False
        window = timedelta(seconds=settings.PURCHASE_CARD_PRESENTATION_SECONDS)
        return purchase.waiting_since >= timezone.now() - window

    def get_total(self, purchase) -> str:
        if purchase.total is not None:
            return f"{purchase.total:.2f}"
        total = sum((i.good.price * i.quantity for i in purchase.items.all()), Decimal("0.00"))
        return f"{total:.2f}"

    def get_balance_after(self, purchase) -> str | None:
        txn = purchase.account_transaction
        if txn is None:
            return None
        if purchase.status == "CONFIRMED" and -txn.amount == purchase.total:
            return f"{txn.balance_after:.2f}"  # never changed since it was paid
        from apps.finance.models import AccountTransaction

        last = (
            AccountTransaction.objects.filter(
                account_id=txn.account_id, reference=f"purchase:{purchase.pk}"
            )
            .order_by("-created_at", "-id")
            .values_list("balance_after", flat=True)
            .first()
        )
        return f"{last:.2f}"

    def get_currency(self, obj) -> str:
        return settings.CURRENCY

    def get_reader_name(self, obj) -> str | None:
        reader = obj.reader
        return (reader.name or reader.code) if reader else None


def _readers():
    from apps.rfid.models import DevicePurpose, RFIDDevice

    # Card assign (master) readers too: court bookings may use them (checked in services).
    return RFIDDevice.objects.filter(
        purpose__in=[DevicePurpose.TILL, DevicePurpose.ENROLL], is_active=True
    )


@extend_schema_field(OpenApiTypes.STR)
class ReaderField(serializers.SlugRelatedField):
    """A till reader (or, for court bookings, a card assign reader) by its code."""

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
        help_text="Code of one of the seller's till readers (e.g. Reader2). Left out: the "
        "seller's only reader, if it has exactly one.",
    )

    def validate_service_position(self, position):
        own = self.context.get("own_seller")
        positions = self.context.get("own_positions")
        if (own is not None and position.seller_id != own.pk) or (
            positions is not None and position.pk not in positions
        ):
            raise serializers.ValidationError(_("You can only sell at your own counters."))
        return position


class SetReaderSerializer(serializers.Serializer):
    reader = ReaderField(allow_null=True)


class TillReaderSerializer(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()
    online = serializers.BooleanField(source="is_online")
    last_seen_at = serializers.DateTimeField()


class ReadersQuerySerializer(PurchaseCreateSerializer):
    """`?service_position=`: the counter whose seller's readers are listed."""

    reader = None


class ItemAddSerializer(serializers.Serializer):
    good = serializers.PrimaryKeyRelatedField(queryset=Good.objects.all())
    quantity = serializers.IntegerField(min_value=1, max_value=999, default=1)


class ItemUpdateSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, max_value=999)


class PaidLineSerializer(serializers.Serializer):
    # Deleted or deactivated goods too: a line already paid keeps its good.
    good = serializers.PrimaryKeyRelatedField(queryset=Good.all_objects.all())
    quantity = serializers.IntegerField(min_value=1, max_value=999)


class PaidChangeSerializer(serializers.Serializer):
    items = PaidLineSerializer(
        many=True,
        allow_empty=False,
        help_text="The whole new list of lines; a line left out is removed.",
    )
    pin = serializers.CharField(
        write_only=True,
        max_length=PIN_LENGTH,
        required=False,
        allow_blank=True,
        help_text="The developer's PIN; needed only when the new total is higher.",
    )

    def validate_items(self, items):
        goods = [line["good"].pk for line in items]
        if len(goods) != len(set(goods)):
            raise serializers.ValidationError(_("Each good can only be listed once."))
        return items


class ConfirmSerializer(serializers.Serializer):
    card_uid = UIDField(
        required=False,
        help_text="Leave out: the card tapped on the counter's reader is used. Typed UIDs are "
        "accepted only if the server allows manual entry.",
    )
    pin = serializers.CharField(
        write_only=True, max_length=PIN_LENGTH, help_text="PIN typed by the developer."
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
