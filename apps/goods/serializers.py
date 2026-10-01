from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.sellers.models import ServicePosition
from apps.sellers.serializers import SellerSummarySerializer

from .models import Good, GoodImage, GoodKind, InventoryMovement, MovementKind, RentalSettings


class GoodImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = GoodImage
        fields = ["id", "image", "alt_text", "position", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_image(self, value):
        if value.size > settings.GOOD_IMAGE_MAX_BYTES:
            limit = settings.GOOD_IMAGE_MAX_BYTES // (1024 * 1024)
            raise serializers.ValidationError(
                _("Images must be at most %(limit)s MB.") % {"limit": limit}
            )
        return value


class RentalSettingsSerializer(serializers.ModelSerializer):
    weekdays = serializers.ListField(
        child=serializers.IntegerField(min_value=0, max_value=6),
        allow_empty=False,
        required=False,
        help_text="Open days: 0 = Monday ... 6 = Sunday. Default: every day.",
    )

    class Meta:
        model = RentalSettings
        fields = [
            "slot_minutes",
            "opening_time",
            "closing_time",
            "weekdays",
            "max_slots_per_booking",
            "max_slots_per_day",
            "max_days_ahead",
        ]

    def validate_weekdays(self, value):
        return sorted(set(value))

    def validate(self, attrs):
        opening = attrs.get("opening_time", getattr(self.instance, "opening_time", None))
        closing = attrs.get("closing_time", getattr(self.instance, "closing_time", None))
        if opening and closing and closing <= opening:
            raise serializers.ValidationError(
                {"closing_time": [_("Must be after the opening time (same day).")]}
            )
        return attrs


class PositionSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = ServicePosition
        fields = ["id", "name"]


class GoodSerializer(serializers.ModelSerializer):
    service_position = serializers.PrimaryKeyRelatedField(queryset=ServicePosition.objects.all())
    position_detail = PositionSummarySerializer(source="service_position", read_only=True)
    seller = SellerSummarySerializer(source="service_position.seller", read_only=True)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    currency = serializers.SerializerMethodField()
    images = GoodImageSerializer(many=True, read_only=True)
    kind = serializers.ChoiceField(choices=GoodKind.choices, default=GoodKind.PRODUCT)
    rental = RentalSettingsSerializer(
        required=False, allow_null=True, help_text="Required for RENTAL goods, else omit."
    )
    track_stock = serializers.BooleanField(
        required=False, help_text="PRODUCT only (default true). Always false otherwise."
    )
    initial_quantity = serializers.IntegerField(
        min_value=0,
        required=False,
        write_only=True,
        help_text="Starting stock (create only; later changes go through /stock/).",
    )

    class Meta:
        model = Good
        fields = [
            "id",
            "service_position",
            "position_detail",
            "seller",
            "name",
            "kind",
            "description",
            "sku",
            "price",
            "currency",
            "is_active",
            "track_stock",
            "quantity",
            "initial_quantity",
            "rental",
            "images",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "quantity", "created_at", "updated_at"]

    def get_currency(self, obj) -> str:
        return settings.CURRENCY

    def validate_service_position(self, position):
        own = self.context.get("own_seller")
        if own is not None and position.seller_id != own.pk:
            raise serializers.ValidationError(_("You can only use your own service positions."))
        if self.instance is not None and position.seller_id != self.instance.seller_id:
            raise serializers.ValidationError(_("A good cannot move to another seller."))
        return position

    def validate(self, attrs):
        errors = {}
        if self.instance is not None:
            if "initial_quantity" in attrs:
                errors["initial_quantity"] = [
                    _("Only allowed when creating; use the stock endpoint.")
                ]
            if "kind" in attrs and attrs["kind"] != self.instance.kind:
                errors["kind"] = [_("The kind of a good cannot be changed; create a new good.")]
            kind = self.instance.kind
        else:
            kind = attrs.get("kind", GoodKind.PRODUCT)

        if kind == GoodKind.PRODUCT:
            if attrs.get("rental"):
                errors["rental"] = [_("Only RENTAL goods have rental settings.")]
        else:
            if attrs.get("track_stock"):
                errors["track_stock"] = [_("%(kind)s goods have no stock.") % {"kind": kind}]
            if self.instance is None:
                attrs["track_stock"] = False
            if attrs.get("initial_quantity"):
                errors["initial_quantity"] = [_("%(kind)s goods have no stock.") % {"kind": kind}]
            if kind == GoodKind.SERVICE and attrs.get("rental"):
                errors["rental"] = [_("Only RENTAL goods have rental settings.")]
            if kind == GoodKind.RENTAL and self.instance is None and not attrs.get("rental"):
                errors["rental"] = [_("Rental settings are required for RENTAL goods.")]
        price = attrs.get("price", getattr(self.instance, "price", None))
        if kind == GoodKind.RENTAL and price is not None and price <= 0:
            errors["price"] = [_("Rentals need a price above 0 (charged per slot).")]
        if errors:
            raise serializers.ValidationError(errors)

        track = attrs.get("track_stock", self.instance.track_stock if self.instance else True)
        if attrs.get("initial_quantity") and not track:
            raise serializers.ValidationError(
                {"initial_quantity": [_("Goods that don't track stock have no quantity.")]}
            )
        sku = attrs.get("sku", "").strip()
        if sku:
            position = attrs.get("service_position") or self.instance.service_position
            clash = Good.objects.filter(sku__iexact=sku, service_position__seller=position.seller)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"sku": [_("This seller already uses this SKU.")]}
                )
            attrs["sku"] = sku
        return attrs


class StockChangeSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(
        choices=[MovementKind.RESTOCK, MovementKind.DAMAGE, MovementKind.ADJUSTMENT]
    )
    quantity = serializers.IntegerField(
        min_value=1, required=False, help_text="RESTOCK / DAMAGE: how many units."
    )
    counted_quantity = serializers.IntegerField(
        min_value=0, required=False, help_text="ADJUSTMENT: the physically counted stock."
    )
    reason = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        kind = attrs["kind"]
        if kind == MovementKind.ADJUSTMENT:
            if attrs.get("counted_quantity") is None:
                raise serializers.ValidationError(
                    {"counted_quantity": [_("Required for ADJUSTMENT.")]}
                )
            attrs.pop("quantity", None)
        else:
            if attrs.get("quantity") is None:
                raise serializers.ValidationError(
                    {"quantity": [_("Required for %(kind)s.") % {"kind": kind}]}
                )
            attrs.pop("counted_quantity", None)
        if kind in (MovementKind.DAMAGE, MovementKind.ADJUSTMENT) and not attrs["reason"].strip():
            raise serializers.ValidationError(
                {"reason": [_("A reason is required for %(kind)s.") % {"kind": kind}]}
            )
        return attrs


class InventoryMovementSerializer(serializers.ModelSerializer):
    good_name = serializers.CharField(source="good.name", read_only=True)

    class Meta:
        model = InventoryMovement
        fields = [
            "id",
            "good",
            "good_name",
            "kind",
            "quantity_delta",
            "quantity_after",
            "reason",
            "reference",
            "actor",
            "created_at",
        ]
        read_only_fields = fields
