from django.contrib.auth import password_validation
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from .models import Role, User, username_validator


class RoleSerializer(serializers.ModelSerializer):
    permissions = serializers.SlugRelatedField(slug_field="codename", many=True, read_only=True)

    class Meta:
        model = Role
        fields = ["id", "code", "name", "description", "is_system", "permissions"]


class UserSerializer(serializers.ModelSerializer):
    roles = serializers.SlugRelatedField(slug_field="code", many=True, read_only=True)

    class Meta:
        model = User
        fields = ["id", "username", "full_name", "is_active", "roles", "date_joined", "last_login"]
        read_only_fields = fields


class MeSerializer(UserSerializer):
    permissions = serializers.SerializerMethodField()

    class Meta(UserSerializer.Meta):
        fields = [*UserSerializer.Meta.fields, "permissions"]
        read_only_fields = fields

    def get_permissions(self, user) -> list[str]:
        return sorted(user.rbac_permissions)


def _unique_username(value: str, user: User | None = None) -> str:
    value = value.strip().lower()
    clash = User.objects.filter(username__iexact=value)
    if user is not None:
        clash = clash.exclude(pk=user.pk)
    if clash.exists():
        raise serializers.ValidationError(_("A user with this username already exists."))
    return value


class UserCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, validators=[username_validator])
    full_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate_username(self, value):
        return _unique_username(value)

    def validate(self, attrs):
        candidate = User(username=attrs["username"], full_name=attrs.get("full_name", ""))
        password_validation.validate_password(attrs["password"], candidate)
        return attrs


class UserUpdateSerializer(serializers.Serializer):
    username = serializers.CharField(
        max_length=150, required=False, validators=[username_validator]
    )
    full_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)

    def validate_username(self, value):
        return _unique_username(value, self.context.get("user"))


class RoleAssignSerializer(serializers.Serializer):
    role = serializers.SlugRelatedField(slug_field="code", queryset=Role.objects.all())


class PasswordChangeSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_old_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError(_("Current password is incorrect."))
        return value

    def validate_new_password(self, value):
        password_validation.validate_password(value, self.context["request"].user)
        return value


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()
