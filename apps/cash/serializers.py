from rest_framework import serializers

from apps.finance.models import MONEY


class StoreSalesSerializer(serializers.Serializer):
    card_total = serializers.DecimalField(
        **MONEY, help_text="Paid by card: what finance pays the store."
    )
    card_count = serializers.IntegerField()
    cash_total = serializers.DecimalField(**MONEY, help_text="Paid in cash: the seller has it.")
    cash_count = serializers.IntegerField()
    total = serializers.DecimalField(**MONEY)


class CashStoreSerializer(StoreSalesSerializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    accepts_cash = serializers.BooleanField()
    price_changes = serializers.IntegerField(help_text="Prices changed and goods added.")


class CashDaySerializer(StoreSalesSerializer):
    date = serializers.DateField()


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
