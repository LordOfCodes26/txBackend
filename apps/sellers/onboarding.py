"""New store in one step: the store's login, the seller, its first counter, and its till
reader assigned to it (a reader registered earlier, not yet assigned). All or nothing.

Each part goes through the same serializer and service as its own form, so the rules
(unique names, password rules, SELLER role, unique reader ID, ...) are the same.
"""

from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.accounts import services as accounts
from apps.accounts.models import Role, User
from apps.accounts.rbac import Roles
from apps.accounts.serializers import UserCreateSerializer
from apps.rfid import services as rfid
from apps.rfid.models import Building, DevicePurpose, RFIDDevice

from . import services
from .serializers import SellerSerializer, ServicePositionSerializer

LOGIN_MODES = ("new", "existing", "none")


class StoreSerializer(serializers.Serializer):
    """Field names are unique across the parts, so the form can show each error at its field."""

    # Store
    name = serializers.CharField(max_length=255)
    contact_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=50, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    # Login: a new SELLER user, an existing one, or none yet
    login = serializers.ChoiceField(choices=LOGIN_MODES, default="new")
    username = serializers.CharField(max_length=150, required=False, allow_blank=True)
    password = serializers.CharField(required=False, allow_blank=True, write_only=True)
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True), required=False, allow_null=True
    )
    # First counter
    counter_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    building = serializers.PrimaryKeyRelatedField(
        queryset=Building.objects.all(), required=False, allow_null=True
    )
    location = serializers.CharField(max_length=255, required=False, allow_blank=True)
    # Till reader (optional): one registered earlier and not assigned to a seller yet
    till_reader = serializers.PrimaryKeyRelatedField(
        queryset=RFIDDevice.objects.filter(purpose=DevicePurpose.TILL),
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        errors = {}

        def check(serializer, rename=None):
            if not serializer.is_valid():
                for field, messages in serializer.errors.items():
                    errors[(rename or {}).get(field, field)] = messages
            return getattr(serializer, "validated_data", {})

        mode = attrs.get("login", "new")
        if mode == "new":
            for field in ("username", "password"):
                if not attrs.get(field):
                    errors[field] = [_("This field is required.")]
            if not errors:
                check(
                    UserCreateSerializer(
                        data={
                            "username": attrs["username"],
                            "password": attrs["password"],
                            "full_name": attrs.get("contact_name") or attrs["name"],
                        }
                    )
                )
        elif mode == "existing" and not attrs.get("user"):
            errors["user"] = [_("Choose the store's login.")]

        seller = {
            k: attrs[k] for k in ("name", "contact_name", "email", "phone", "notes") if k in attrs
        }
        if mode == "existing" and attrs.get("user"):
            seller["user"] = attrs["user"].pk
        check(SellerSerializer(data=seller))

        counter = {
            "name": attrs.get("counter_name") or attrs["name"],
            "location": attrs.get("location", ""),
            "building": attrs["building"].pk if attrs.get("building") else None,
        }
        check(ServicePositionSerializer(data=counter), {"name": "counter_name"})
        errors.pop("seller", None)  # the store doesn't exist yet: its counter is its first

        reader = attrs.get("till_reader")
        if reader is not None and reader.seller_id is not None:
            errors["till_reader"] = [
                _("This reader is already assigned to %(seller)s.") % {"seller": reader.seller.name}
            ]

        if errors:
            raise serializers.ValidationError(errors)
        return attrs


def required_permissions(data: dict) -> list[str]:
    """seller.create always; making a login needs user.manage, a reader reader.manage."""
    needed = ["seller.create", "counter.manage"]
    if data.get("login", "new") == "new":
        needed.append("user.manage")
    if data.get("till_reader"):
        needed.append("reader.manage")
    return needed


@transaction.atomic
def create_store(*, actor, data: dict) -> dict:
    user = data.get("user") if data.get("login") == "existing" else None
    if data.get("login", "new") == "new":
        user = accounts.create_user(
            actor=actor,
            username=data["username"],
            password=data["password"],
            full_name=data.get("contact_name") or data["name"],
        )
        accounts.assign_role(actor=actor, user=user, role=Role.objects.get(code=Roles.SELLER))
    seller = services.create_seller(
        actor=actor,
        name=data["name"].strip(),
        contact_name=data.get("contact_name", ""),
        email=data.get("email", ""),
        phone=data.get("phone", ""),
        notes=data.get("notes", ""),
        user=user,
    )
    position = services.create_position(
        actor=actor,
        seller=seller,
        name=(data.get("counter_name") or data["name"]).strip(),
        location=data.get("location", ""),
        building=data.get("building"),
    )
    reader = data.get("till_reader")
    if reader is not None:
        rfid.assign_reader(actor=actor, device=reader, seller=seller)
    return {
        "seller": seller.pk,
        "position": position.pk,
        "user": user.pk if user else None,
        "username": user.username if user else None,
        "reader": reader.pk if reader else None,
    }
