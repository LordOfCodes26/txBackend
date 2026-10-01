from decimal import Decimal

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.developers.models import Developer
from apps.developers.serializers import DeveloperSummarySerializer

from .models import AccountTransaction, DeveloperAccount


class DeveloperAccountSerializer(serializers.ModelSerializer):
    developer = DeveloperSummarySerializer(read_only=True)
    currency = serializers.SerializerMethodField()

    class Meta:
        model = DeveloperAccount
        fields = [
            "id",
            "developer",
            "balance",
            "currency",
            "status",
            "status_reason",
            "has_pin",
            "pin_locked_until",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_currency(self, obj) -> str:
        return settings.CURRENCY


class AccountTransactionSerializer(serializers.ModelSerializer):
    developer = DeveloperSummarySerializer(source="account.developer", read_only=True)

    class Meta:
        model = AccountTransaction
        fields = [
            "id",
            "account",
            "developer",
            "kind",
            "amount",
            "balance_after",
            "description",
            "reference",
            "actor",
            "created_at",
        ]
        read_only_fields = fields


class DepositSerializer(serializers.Serializer):
    developer = serializers.PrimaryKeyRelatedField(queryset=Developer.objects.all())
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    description = serializers.CharField(max_length=255, required=False, allow_blank=True)


class AdjustmentSerializer(serializers.Serializer):
    developer = serializers.PrimaryKeyRelatedField(queryset=Developer.all_objects.all())
    amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, help_text="Signed: positive credits, negative debits."
    )
    reason = serializers.CharField(max_length=255)

    def validate_amount(self, value):
        if value == 0:
            raise serializers.ValidationError(_("Amount cannot be zero."))
        return value


class StatusChangeSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")


class NewPinSerializer(serializers.Serializer):
    """A PIN the developer types twice (4-6 digits, not trivial like 1111 or 1234)."""

    pin = serializers.CharField(write_only=True, max_length=6)
    pin_confirm = serializers.CharField(write_only=True, max_length=6)

    def validate(self, attrs):
        if attrs["pin"] != attrs["pin_confirm"]:
            raise serializers.ValidationError({"pin_confirm": [_("The PINs don't match.")]})
        return attrs


class ResetPinSerializer(serializers.Serializer):
    pin = serializers.CharField(
        write_only=True,
        max_length=6,
        required=False,
        help_text="New PIN typed by the developer; leave out to only clear the old one.",
    )
    pin_confirm = serializers.CharField(write_only=True, max_length=6, required=False)

    def validate(self, attrs):
        if attrs.get("pin") and attrs.get("pin") != attrs.get("pin_confirm"):
            raise serializers.ValidationError({"pin_confirm": [_("The PINs don't match.")]})
        return attrs


class SetPinSerializer(serializers.Serializer):
    pin = serializers.CharField(write_only=True, max_length=6)
    current_pin = serializers.CharField(
        write_only=True,
        max_length=6,
        required=False,
        help_text="Required to change an existing PIN.",
    )
