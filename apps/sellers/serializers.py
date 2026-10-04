from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.accounts.models import User
from apps.rfid.models import Building

from .access import has_seller_role
from .models import Seller, ServicePosition


class SellerSummarySerializer(serializers.ModelSerializer):
    class Meta:
        model = Seller
        fields = ["id", "name", "status"]


def _ensure_seller_login(user):
    """Only active users with the SELLER role can run a store or a position."""
    if user is not None and (not user.is_active or not has_seller_role(user)):
        raise serializers.ValidationError(_("This user doesn't have the SELLER role."))
    return user


class SellerSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        required=False,
        allow_null=True,
        help_text="The store's login: a user with the SELLER role. They manage only this store.",
    )
    user_username = serializers.CharField(source="user.username", read_only=True, default=None)

    class Meta:
        model = Seller
        fields = [
            "id",
            "user",
            "user_username",
            "name",
            "contact_name",
            "email",
            "phone",
            "status",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        validators = []

    def _others(self):
        qs = Seller.objects.all()
        return qs.exclude(pk=self.instance.pk) if self.instance else qs

    def validate_name(self, value):
        value = value.strip()
        if self._others().filter(name__iexact=value).exists():
            raise serializers.ValidationError(_("A seller with this name already exists."))
        return value

    def validate_user(self, user):
        _ensure_seller_login(user)
        if user is not None and self._others().filter(user=user).exists():
            raise serializers.ValidationError(_("This user is already linked to another seller."))
        if user is not None and ServicePosition.objects.filter(manager=user).exists():
            raise serializers.ValidationError(
                _("This user manages a sell position and can't also own a seller.")
            )
        return user


class ServicePositionSerializer(serializers.ModelSerializer):
    seller = serializers.PrimaryKeyRelatedField(queryset=Seller.objects.all(), required=False)
    seller_detail = SellerSummarySerializer(source="seller", read_only=True)
    building = serializers.PrimaryKeyRelatedField(
        queryset=Building.objects.all(),
        required=False,
        allow_null=True,
        help_text="Optional: the building the position is in.",
    )
    building_name = serializers.CharField(source="building.name", read_only=True, default=None)
    manager = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True),
        required=False,
        allow_null=True,
        help_text="User id of the position's manager: sees and manages only this position.",
    )
    manager_username = serializers.CharField(
        source="manager.username", read_only=True, default=None
    )

    class Meta:
        model = ServicePosition
        fields = [
            "id",
            "seller",
            "seller_detail",
            "name",
            "location",
            "building",
            "building_name",
            "manager",
            "manager_username",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        validators = []

    def validate_manager(self, user):
        _ensure_seller_login(user)
        if user is not None and Seller.objects.filter(user=user).exists():
            raise serializers.ValidationError(
                _("This user is a seller's owner and can't also manage a position.")
            )
        return user

    def validate(self, attrs):
        if self.context.get("own_positions") is not None:
            for field in ("manager", "building", "is_active"):
                if field in attrs:
                    raise serializers.ValidationError(
                        {field: [_("Only the seller's owner or staff can change this.")]}
                    )
        own = self.context.get("own_seller")
        if self.instance is not None:
            if "seller" in attrs and attrs["seller"] != self.instance.seller:
                raise serializers.ValidationError(
                    {"seller": [_("A position cannot move to another seller.")]}
                )
            seller = self.instance.seller
        elif own is not None:
            if attrs.get("seller") not in (None, own):
                raise serializers.ValidationError(
                    {"seller": [_("You can only create positions for your own seller.")]}
                )
            seller = attrs["seller"] = own
        else:
            seller = attrs.get("seller")
            if seller is None:
                raise serializers.ValidationError({"seller": [_("This field is required.")]})

        name = attrs.get("name")
        if name:
            clash = ServicePosition.objects.filter(seller=seller, name__iexact=name.strip())
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"name": [_("This seller already has a position with this name.")]}
                )
        return attrs
