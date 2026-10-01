from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from apps.accounts.models import User
from apps.rfid.models import Building

from .models import Developer


class DeveloperSummarySerializer(serializers.ModelSerializer):
    """Compact developer reference embedded by other modules (RFID, attendance, ...)."""

    class Meta:
        model = Developer
        fields = ["id", "employee_number", "full_name", "department"]


class DeveloperSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), required=False, allow_null=True
    )
    manager = serializers.PrimaryKeyRelatedField(
        queryset=Developer.objects.all(), required=False, allow_null=True
    )
    manager_detail = DeveloperSummarySerializer(source="manager", read_only=True)
    building = serializers.PrimaryKeyRelatedField(
        queryset=Building.objects.all(),
        required=False,
        allow_null=True,
        help_text="Home building (optional). Building managers must set their own building.",
    )
    building_name = serializers.CharField(source="building.name", read_only=True, default=None)

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
            "manager",
            "manager_detail",
            "start_date",
            "out_date",
            "status",
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

    def validate_manager(self, manager):
        if manager is None or self.instance is None:
            return manager
        # Walk up the chain: the new manager must not report (directly or not) to us.
        seen = set()
        node = manager
        while node is not None and node.pk not in seen:
            if node.pk == self.instance.pk:
                raise serializers.ValidationError(_("A developer cannot report to themselves."))
            seen.add(node.pk)
            node = node.manager
        return manager

    def validate(self, attrs):
        start = attrs.get("start_date", getattr(self.instance, "start_date", None))
        out = attrs.get("out_date", getattr(self.instance, "out_date", None))
        if start and out and out < start:
            raise serializers.ValidationError(
                {"out_date": [_("The out date cannot be before the start date.")]}
            )
        return attrs


class MyDeveloperProfileSerializer(serializers.ModelSerializer):
    manager = DeveloperSummarySerializer(read_only=True)

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
            "manager",
            "start_date",
            "out_date",
            "status",
        ]
        read_only_fields = fields
