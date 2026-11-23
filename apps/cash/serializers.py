from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.finance.models import MONEY
from apps.goods.models import Good
from apps.goods.serializers import QuantityField
from apps.purchases.models import Purchase
from apps.sellers.models import Seller

from .models import CashEntry, CashRequest, CountOutcome, StockCount, StockCountLine


def _reason(**kwargs):
    return serializers.CharField(max_length=255, **kwargs)


class CashStoreSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    accepts_cash = serializers.BooleanField()
    opening = serializers.DecimalField(**MONEY, help_text="Owed before the period.")
    sales = serializers.DecimalField(**MONEY, help_text="Cash sales, ones deleted later too.")
    sales_count = serializers.IntegerField()
    returned = serializers.DecimalField(**MONEY, help_text="Cash returned in the period.")
    returned_count = serializers.IntegerField()
    expected = serializers.DecimalField(**MONEY, help_text="Cash from sales: sales - returned.")
    handed_over = serializers.DecimalField(**MONEY)
    charged = serializers.DecimalField(**MONEY, help_text="Missing goods charged to the store.")
    closing = serializers.DecimalField(**MONEY, help_text="Owed at the end of the period.")
    owed = serializers.DecimalField(**MONEY, help_text="Owed now.")
    last_handover_at = serializers.DateTimeField(allow_null=True)
    pending_requests = serializers.IntegerField()
    open_counts = serializers.IntegerField(help_text="Stock counts still to be explained.")
    open_missing = serializers.DecimalField(**MONEY, help_text="The missing goods' value.")
    last_count_at = serializers.DateTimeField(allow_null=True)


class CashDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    sales = serializers.DecimalField(**MONEY)
    returned = serializers.DecimalField(**MONEY)
    expected = serializers.DecimalField(**MONEY)
    handed_over = serializers.DecimalField(**MONEY)
    charged = serializers.DecimalField(**MONEY)
    owed = serializers.DecimalField(**MONEY, help_text="Owed at the end of the day.")


class CashOverviewSerializer(serializers.Serializer):
    currency = serializers.CharField()
    stores = CashStoreSerializer(many=True)


class CashStoreDetailSerializer(CashStoreSerializer):
    currency = serializers.CharField()
    daily = CashDaySerializer(many=True)


class CashEntrySerializer(serializers.ModelSerializer):
    seller_name = serializers.CharField(source="seller.name", read_only=True)
    actor_username = serializers.CharField(source="actor.username", read_only=True, default=None)
    reversed = serializers.SerializerMethodField(help_text="Cancelled by a later entry.")
    purchase = serializers.SerializerMethodField(help_text="Sales and returns: the purchase.")

    class Meta:
        model = CashEntry
        fields = [
            "id",
            "seller",
            "seller_name",
            "kind",
            "amount",
            "owed_after",
            "note",
            "reference",
            "purchase",
            "reverses",
            "reversed",
            "actor_username",
            "created_at",
        ]
        read_only_fields = fields

    def get_reversed(self, entry) -> bool:
        return getattr(entry, "reversal", None) is not None

    def get_purchase(self, entry) -> int | None:
        kind, _sep, pk = entry.reference.partition(":")
        return int(pk) if kind == "purchase" and pk.isdigit() else None


@extend_schema_field(OpenApiTypes.INT)
class SellerField(serializers.PrimaryKeyRelatedField):
    """One of the stores the user may use this control on (`context["sellers"]`)."""

    def get_queryset(self):
        return self.context.get("sellers", Seller.objects.none())


class HandoverSerializer(serializers.Serializer):
    seller = SellerField()
    amount = serializers.DecimalField(**MONEY, min_value=0)
    note = _reason(required=False, allow_blank=True, default="")

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError(_("The amount must be greater than zero."))
        return value


class ReasonSerializer(serializers.Serializer):
    reason = _reason()


class DecisionSerializer(serializers.Serializer):
    note = _reason(required=False, allow_blank=True, default="")


class RejectionSerializer(serializers.Serializer):
    note = _reason(help_text="Why not.")


@extend_schema_field(OpenApiTypes.INT)
class PurchaseField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        return self.context.get("purchases", Purchase.objects.none())


class DeletionRequestSerializer(serializers.Serializer):
    purchase = PurchaseField()
    reason = _reason()


class CashRequestSerializer(serializers.ModelSerializer):
    seller_name = serializers.CharField(source="seller.name", read_only=True)
    good_name = serializers.CharField(source="good.name", read_only=True, default=None)
    store_number = serializers.IntegerField(
        source="purchase.store_number", read_only=True, default=None
    )
    total = serializers.DecimalField(source="purchase.total", read_only=True, default=None, **MONEY)
    buyer_name = serializers.CharField(source="purchase.buyer_name", read_only=True, default=None)
    paid_at = serializers.DateTimeField(
        source="purchase.confirmed_at", read_only=True, default=None
    )
    sold_by = serializers.CharField(
        source="purchase.confirmed_by.username", read_only=True, default=None
    )
    track_stock = serializers.BooleanField(source="good.track_stock", read_only=True, default=None)
    requested_by_username = serializers.CharField(
        source="requested_by.username", read_only=True, default=None
    )
    decided_by_username = serializers.CharField(
        source="decided_by.username", read_only=True, default=None
    )

    class Meta:
        model = CashRequest
        fields = [
            "id",
            "kind",
            "status",
            "seller",
            "seller_name",
            "purchase",
            "store_number",
            "total",
            "buyer_name",
            "paid_at",
            "sold_by",
            "good",
            "good_name",
            "track_stock",
            "old_price",
            "new_price",
            "reason",
            "requested_by",
            "requested_by_username",
            "decided_by_username",
            "decided_at",
            "decision_note",
            "created_at",
        ]
        read_only_fields = fields


class SheetQuerySerializer(serializers.Serializer):
    seller = SellerField()


class SheetGoodSerializer(serializers.Serializer):
    good = serializers.IntegerField(source="pk")
    name = serializers.CharField()
    category_name = serializers.CharField(source="category.name", default=None)
    counter = serializers.CharField(source="service_position.name")
    quantity = QuantityField(help_text="Stock now; send it back as `expected`.")
    decimal_quantity = serializers.BooleanField()
    price = serializers.DecimalField(max_digits=12, decimal_places=2)
    is_active = serializers.BooleanField()


class CountLineInputSerializer(serializers.Serializer):
    good = serializers.PrimaryKeyRelatedField(queryset=Good.objects.all())
    expected = QuantityField(min_value=0)
    counted = QuantityField(min_value=0)


class StockCountInputSerializer(serializers.Serializer):
    seller = SellerField()
    lines = CountLineInputSerializer(many=True, allow_empty=False)
    note = _reason(required=False, allow_blank=True, default="")

    def validate_lines(self, lines):
        goods = [line["good"].pk for line in lines]
        if len(goods) != len(set(goods)):
            raise serializers.ValidationError(_("Each good can only be listed once."))
        return lines


class StockCountLineSerializer(serializers.ModelSerializer):
    good_name = serializers.CharField(source="good.name", read_only=True)
    expected = QuantityField(read_only=True)
    counted = QuantityField(read_only=True)
    difference = QuantityField(read_only=True)

    class Meta:
        model = StockCountLine
        fields = ["good", "good_name", "expected", "counted", "difference", "price"]
        read_only_fields = fields


class StockCountSerializer(serializers.ModelSerializer):
    seller_name = serializers.CharField(source="seller.name", read_only=True)
    counted_by_username = serializers.CharField(
        source="counted_by.username", read_only=True, default=None
    )
    resolved_by_username = serializers.CharField(
        source="resolved_by.username", read_only=True, default=None
    )
    charged = serializers.DecimalField(
        source="charge.amount", read_only=True, default=None, **MONEY
    )
    goods_count = serializers.SerializerMethodField()
    lines = StockCountLineSerializer(many=True, read_only=True)

    class Meta:
        model = StockCount
        fields = [
            "id",
            "seller",
            "seller_name",
            "note",
            "missing_value",
            "surplus_value",
            "outcome",
            "outcome_note",
            "charge",
            "charged",
            "counted_by_username",
            "resolved_by_username",
            "resolved_at",
            "goods_count",
            "lines",
            "created_at",
        ]
        read_only_fields = fields

    def get_goods_count(self, count) -> int:
        return len(count.lines.all())


class ResolveSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(choices=[CountOutcome.EXPLAINED, CountOutcome.CHARGED])
    note = _reason(help_text="The explanation, or why the store pays.")
    amount = serializers.DecimalField(
        **MONEY, required=False, help_text="CHARGED: less than the whole missing value."
    )
