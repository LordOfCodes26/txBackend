from rest_framework import serializers

from apps.accounts.models import User

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

    class Meta:
        model = Developer
        fields = [
            "id",
            "user",
            "employee_number",
            "full_name",
            "email",
            "phone",
            "department",
            "position_title",
            "manager",
            "manager_detail",
            "start_date",
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
            raise serializers.ValidationError("This employee number is already in use.")
        return value

    def validate_email(self, value):
        value = value.lower()
        if self._others().filter(email__iexact=value).exists():
            raise serializers.ValidationError("Another developer already uses this email.")
        return value

    def validate_user(self, user):
        if user is not None and self._others().filter(user=user).exists():
            raise serializers.ValidationError("This user is linked to another developer.")
        return user

    def validate_manager(self, manager):
        if manager is None or self.instance is None:
            return manager
        # Walk up the chain: the new manager must not report (directly or not) to us.
        seen = set()
        node = manager
        while node is not None and node.pk not in seen:
            if node.pk == self.instance.pk:
                raise serializers.ValidationError("A developer cannot report to themselves.")
            seen.add(node.pk)
            node = node.manager
        return manager


class MyDeveloperProfileSerializer(serializers.ModelSerializer):
    manager = DeveloperSummarySerializer(read_only=True)

    class Meta:
        model = Developer
        fields = [
            "id",
            "employee_number",
            "full_name",
            "email",
            "phone",
            "department",
            "position_title",
            "manager",
            "start_date",
            "status",
        ]
        read_only_fields = fields
