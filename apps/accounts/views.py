from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from common.permissions import HasPermissions

from . import services
from .filters import UserFilter
from .models import Role, User
from .serializers import (
    LogoutSerializer,
    MeSerializer,
    PasswordChangeSerializer,
    RoleAssignSerializer,
    RoleSerializer,
    UserCreateSerializer,
    UserSerializer,
    UserUpdateSerializer,
)


class LoginView(TokenObtainPairView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class RefreshView(TokenRefreshView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class LogoutView(APIView):
    """Revoke a refresh token. Works without a valid access token: logout usually happens
    after the access token has expired, and holding the refresh token is proof enough."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    @extend_schema(request=LogoutSerializer, responses={204: None})
    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            token = RefreshToken(serializer.validated_data["refresh"])
        except TokenError as exc:
            raise ValidationError({"refresh": [str(exc)]}) from exc
        user = request.user
        if user.is_authenticated and str(token.get("user_id")) != str(user.pk):
            raise ValidationError({"refresh": [_("Token does not belong to this user.")]})
        token.blacklist()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    @extend_schema(responses=MeSerializer)
    def get(self, request):
        return Response(MeSerializer(request.user).data)


class PasswordChangeView(APIView):
    @extend_schema(request=PasswordChangeSerializer, responses={204: None})
    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        services.change_password(
            user=request.user, new_password=serializer.validated_data["new_password"]
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class UserViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Users are never hard-deleted; deactivate with `PATCH {"is_active": false}`."""

    queryset = User.objects.prefetch_related("roles").order_by("id")
    serializer_class = UserSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["user.view"],
        "retrieve": ["user.view"],
        "create": ["user.manage"],
        "partial_update": ["user.manage"],
        "assign_role": ["role.assign"],
        "remove_role": ["role.assign"],
    }
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filterset_class = UserFilter
    search_fields = ["username", "full_name"]
    ordering_fields = ["username", "full_name", "date_joined", "last_login"]

    @extend_schema(request=UserCreateSerializer, responses={201: UserSerializer})
    def create(self, request, *args, **kwargs):
        serializer = UserCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = services.create_user(actor=request.user, **serializer.validated_data)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=UserUpdateSerializer, responses=UserSerializer)
    def partial_update(self, request, *args, **kwargs):
        user = self.get_object()
        serializer = UserUpdateSerializer(data=request.data, partial=True, context={"user": user})
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data:
            user = services.update_user(actor=request.user, user=user, **serializer.validated_data)
        return Response(UserSerializer(user).data)

    @extend_schema(request=RoleAssignSerializer, responses={201: UserSerializer})
    @action(detail=True, methods=["post"], url_path="roles")
    def assign_role(self, request, pk=None):
        user = self.get_object()
        serializer = RoleAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.assign_role(actor=request.user, user=user, role=serializer.validated_data["role"])
        user = self.get_queryset().get(pk=user.pk)
        return Response(UserSerializer(user).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={204: None})
    @action(detail=True, methods=["delete"], url_path=r"roles/(?P<role_code>[A-Z_]+)")
    def remove_role(self, request, pk=None, role_code=None):
        user = self.get_object()
        role = get_object_or_404(Role, code=role_code)
        services.remove_role(actor=request.user, user=user, role=role)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RoleViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Role.objects.prefetch_related("permissions")
    serializer_class = RoleSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"list": ["role.view"], "retrieve": ["role.view"]}
    lookup_field = "code"
    pagination_class = None
    filter_backends = []
