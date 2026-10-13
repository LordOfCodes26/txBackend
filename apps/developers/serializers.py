from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.accounts.models import User
from apps.rfid.models import Building

from .models import Developer


class PhotoField(serializers.ImageField):
    """The photo's address on this server (`/media/...`), or null. Always relative: the
    browser loads it from the same address as the app (nginx serves /media/)."""

    def __init__(self, **kwargs):
        super().__init__(read_only=True, **kwargs)

    def to_representation(self, value):
        return value.url if value else None


class DeveloperSummarySerializer(serializers.ModelSerializer):
    """Compact developer reference embedded by other modules (RFID, attendance, ...)."""

    photo = PhotoField()

    class Meta:
        model = Developer
        fields = ["id", "employee_number", "full_name", "department", "photo"]


class DeveloperSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), required=False, allow_null=True
    )
    building = serializers.PrimaryKeyRelatedField(
        queryset=Building.objects.all(),
        required=False,
        allow_null=True,
        help_text="Home building (optional). Building managers must set their own building.",
    )
    building_name = serializers.CharField(source="building.name", read_only=True, default=None)
    photo = PhotoField(help_text="Set and removed with POST / DELETE .../photo/.")

    class Meta:
        model = Developer
        fields = [
            "id",
            "user",
            "employee_number",
            "full_name",
            "phone",
            "home_address",
            "birthday",
            "department",
            "position_title",
            "building",
            "building_name",
            "start_date",
            "out_date",
            "status",
            "photo",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        # Uniqueness is partial (alive rows only), so it is checked by hand below.
        validators = []

    def _others(self):
        qs = Developer.objects.all()
        return qs.exclude(pk=self.instance.pk) if self.instance else qs

    def validate_employee_number(self, value):
        value = value.strip()
        if self._others().filter(employee_number=value).exists():
            raise serializers.ValidationError(_("This employee number is already in use."))
        return value

    def validate_birthday(self, value):
        if value is not None and value > timezone.localdate():
            raise serializers.ValidationError(_("Birthday cannot be in the future."))
        return value

    def validate_user(self, user):
        if user is not None and self._others().filter(user=user).exists():
            raise serializers.ValidationError(_("This user is linked to another developer."))
        return user

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        out = attrs.get("out_date", getattr(self.instance, "out_date", None))
        if start and out and out < start:
            raise serializers.ValidationError(
                {"out_date": [_("The out date cannot be before the start date.")]}
            )
        return attrs


class MyDeveloperProfileSerializer(serializers.ModelSerializer):
    photo = PhotoField()

    class Meta:
        model = Developer
        fields = [
            "id",
            "employee_number",
            "full_name",
            "phone",
            "home_address",
            "birthday",
            "department",
            "position_title",
            "start_date",
            "out_date",
            "status",
            "photo",
        ]
        read_only_fields = fields


class DeveloperPhotoSerializer(serializers.Serializer):
    photo = serializers.ImageField(help_text="JPEG, PNG or WebP, at most 5 MB.")

    def validate_photo(self, value):
        if value.size > settings.DEVELOPER_PHOTO_MAX_BYTES:
            limit = settings.DEVELOPER_PHOTO_MAX_BYTES // (1024 * 1024)
            raise serializers.ValidationError(
                _("Images must be at most %(limit)s MB.") % {"limit": limit}
            )
        ext = value.name.rsplit(".", 1)[-1].lower() if "." in value.name else ""
        if ext not in ("jpg", "jpeg", "png", "webp"):
            raise serializers.ValidationError(_("Use a JPEG, PNG or WebP image."))
        return value
