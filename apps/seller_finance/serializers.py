from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from apps.sellers.models import Seller
from apps.sellers.serializers import SellerSummarySerializer

from .models import SellerAccount, SellerPayment, SellerTransaction


class SellerAccountSerializer(serializers.ModelSerializer):
    seller = SellerSummarySerializer(read_only=True)
    reserved = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    available_balance = serializers.SerializerMethodField()
    currency = serializers.SerializerMethodField()

    class Meta:
        model = SellerAccount
        fields = [
            "id",
            "seller",
            "balance",
            "reserved",
            "available_balance",
            "currency",
            "updated_at",
        ]
        read_only_fields = fields

    def get_available_balance(self, obj) -> str:
        return f"{obj.balance - obj.reserved:.2f}"

    def get_currency(self, obj) -> str:
        return settings.CURRENCY


class SellerTransactionSerializer(serializers.ModelSerializer):
    seller = SellerSummarySerializer(source="account.seller", read_only=True)

    class Meta:
        model = SellerTransaction
        fields = [
            "id",
            "account",
            "seller",
            "kind",
            "amount",
            "balance_after",
            "description",
            "reference",
            "actor",
            "created_at",
        ]
        read_only_fields = fields


class SellerPaymentSerializer(serializers.ModelSerializer):
    seller = SellerSummarySerializer(read_only=True)
    currency = serializers.SerializerMethodField()

    class Meta:
        model = SellerPayment
        fields = [
            "id",
            "seller",
            "amount",
            "currency",
            "status",
            "note",
            "requested_by",
            "created_at",
            "approved_by",
            "approved_at",
            "processed_at",
            "paid_by",
            "paid_at",
            "payment_reference",
            "rejected_by",
            "rejected_at",
            "rejection_reason",
            "cancelled_at",
        ]
        read_only_fields = fields

    def get_currency(self, obj) -> str:
        return settings.CURRENCY


class PayoutRequestSerializer(serializers.Serializer):
    seller = serializers.PrimaryKeyRelatedField(
        queryset=Seller.objects.all(),
        required=False,
        help_text="Required for finance staff; sellers always request for themselves.",
    )
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    note = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        own = self.context.get("own_seller")
        if own is not None:
            if attrs.get("seller") not in (None, own):
                raise serializers.ValidationError(
                    {"seller": ["You can only request payouts for your own seller."]}
                )
            attrs["seller"] = own
        elif attrs.get("seller") is None:
            raise serializers.ValidationError({"seller": ["This field is required."]})
        return attrs


class PayReferenceSerializer(serializers.Serializer):
    payment_reference = serializers.CharField(
        max_length=100, help_text="Bank transfer or cash receipt number."
    )


class RejectSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255)


class SellerAdjustmentSerializer(serializers.Serializer):
    seller = serializers.PrimaryKeyRelatedField(queryset=Seller.objects.all())
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)
    reason = serializers.CharField(max_length=255)

    def validate_amount(self, value):
        if value == 0:
            raise serializers.ValidationError("Amount cannot be zero.")
        return value
