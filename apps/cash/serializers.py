from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.finance.models import MONEY
from apps.sellers.models import Seller

from .models import CashEntry


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
    closing = serializers.DecimalField(**MONEY, help_text="Owed at the end of the period.")
    owed = serializers.DecimalField(**MONEY, help_text="Owed now.")
    last_handover_at = serializers.DateTimeField(allow_null=True)
    price_changes = serializers.IntegerField(help_text="Prices changed and goods added.")


class CashDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    sales = serializers.DecimalField(**MONEY)
    returned = serializers.DecimalField(**MONEY)
    expected = serializers.DecimalField(**MONEY)
    handed_over = serializers.DecimalField(**MONEY)
    owed = serializers.DecimalField(**MONEY, help_text="Owed at the end of the day.")


class CashOverviewSerializer(serializers.Serializer):
    currency = serializers.CharField()
    stores = CashStoreSerializer(many=True)


class CashStoreDetailSerializer(CashStoreSerializer):
    currency = serializers.CharField()
    daily = CashDaySerializer(many=True)


class PriceChangeSerializer(serializers.Serializer):
    """A good's price changed, or a good added (`old_price` null)."""

    id = serializers.IntegerField()
    good = serializers.IntegerField()
    good_name = serializers.CharField()
    seller = serializers.IntegerField()
    seller_name = serializers.CharField()
    track_stock = serializers.BooleanField()
    old_price = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    new_price = serializers.DecimalField(max_digits=12, decimal_places=2)
    by = serializers.CharField()
    by_store = serializers.BooleanField(help_text="By someone who works for the store.")
    created_at = serializers.DateTimeField()


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
