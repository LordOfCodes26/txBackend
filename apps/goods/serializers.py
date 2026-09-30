from django.conf import settings
from rest_framework import serializers

from apps.sellers.models import ServicePosition
from apps.sellers.serializers import SellerSummarySerializer

from .models import Good, GoodImage, InventoryMovement, MovementKind


class GoodImageSerializer(serializers.ModelSerializer):
    class Meta:
        model = GoodImage
        fields = ["id", "image", "alt_text", "position", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_image(self, value):
        if value.size > settings.GOOD_IMAGE_MAX_BYTES:
            limit = settings.GOOD_IMAGE_MAX_BYTES // (1024 * 1024)
            raise serializers.ValidationError(f"Images must be at most {limit} MB.")
        return value


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
            "description",
            "sku",
            "price",
            "currency",
            "is_active",
            "track_stock",
            "quantity",
            "initial_quantity",
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
            raise serializers.ValidationError("You can only use your own service positions.")
        if self.instance is not None and position.seller_id != self.instance.seller_id:
            raise serializers.ValidationError("A good cannot move to another seller.")
        return position

    def validate(self, attrs):
        if self.instance is not None and "initial_quantity" in attrs:
            raise serializers.ValidationError(
                {"initial_quantity": ["Only allowed when creating; use the stock endpoint."]}
            )
        track = attrs.get("track_stock", self.instance.track_stock if self.instance else True)
        if attrs.get("initial_quantity") and not track:
            raise serializers.ValidationError(
                {"initial_quantity": ["Goods that don't track stock have no quantity."]}
            )
        sku = attrs.get("sku", "").strip()
        if sku:
            position = attrs.get("service_position") or self.instance.service_position
            clash = Good.objects.filter(sku__iexact=sku, service_position__seller=position.seller)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError({"sku": ["This seller already uses this SKU."]})
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
                    {"counted_quantity": ["Required for ADJUSTMENT."]}
                )
            attrs.pop("quantity", None)
        else:
            if attrs.get("quantity") is None:
                raise serializers.ValidationError({"quantity": [f"Required for {kind}."]})
            attrs.pop("counted_quantity", None)
        if kind in (MovementKind.DAMAGE, MovementKind.ADJUSTMENT) and not attrs["reason"].strip():
            raise serializers.ValidationError({"reason": [f"A reason is required for {kind}."]})
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
