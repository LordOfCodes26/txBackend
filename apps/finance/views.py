from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.developers.exceptions import DeveloperProfileNotFound
from apps.developers.models import Developer
from common.idempotency import HEADER, require_idempotency_key
from common.permissions import HasPermissions

from . import services
from .filters import AccountTransactionFilter, DeveloperAccountFilter
from .models import AccountTransaction, DeveloperAccount
from .serializers import (
    AccountTransactionSerializer,
    AdjustmentSerializer,
    DepositSerializer,
    DeveloperAccountSerializer,
    ResetPinSerializer,
    SetPinSerializer,
    StatusChangeSerializer,
)

IDEMPOTENCY_PARAM = OpenApiParameter(
    HEADER,
    location=OpenApiParameter.HEADER,
    required=True,
    description="Unique per user action (use a UUID). Retries with the same key are safe.",
)


def _own_developer(request) -> Developer:
    developer = Developer.objects.filter(user=request.user).first()
    if developer is None:
        raise DeveloperProfileNotFound()
    return developer


class DeveloperAccountViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = DeveloperAccount.objects.select_related("developer")
    serializer_class = DeveloperAccountSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["finance.view"],
        "retrieve": ["finance.view"],
        "me": [],
        "set_my_pin": [],
        "reset_pin": ["finance.adjust"],
        "freeze": ["finance.adjust"],
        "unfreeze": ["finance.adjust"],
        "close": ["finance.adjust"],
        "reopen": ["finance.adjust"],
    }
    filterset_class = DeveloperAccountFilter
    search_fields = ["developer__full_name", "developer__employee_number"]
    ordering_fields = ["balance", "developer__full_name", "updated_at"]

    @extend_schema(responses=DeveloperAccountSerializer)
    @action(detail=False, methods=["get"])
    def me(self, request):
        account = services.open_account(_own_developer(request))
        return Response(DeveloperAccountSerializer(account).data)

    @extend_schema(request=SetPinSerializer, responses={204: None})
    @action(detail=False, methods=["post"], url_path="me/pin")
    def set_my_pin(self, request):
        """Set your purchase PIN (4-6 digits), or change it by also sending `current_pin`."""
        serializer = SetPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = services.open_account(_own_developer(request))
        services.set_pin(
            actor=request.user,
            account=account,
            pin=serializer.validated_data["pin"],
            current_pin=serializer.validated_data.get("current_pin"),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=ResetPinSerializer, responses=DeveloperAccountSerializer)
    @action(detail=True, methods=["post"], url_path="reset-pin")
    def reset_pin(self, request, pk=None):
        """Replace a forgotten PIN: the developer types the new one twice (`pin`,
        `pin_confirm`). Without `pin`, the PIN is only cleared. Clears any lockout."""
        serializer = ResetPinSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = self.get_object()
        services.reset_pin(
            actor=request.user, account=account, pin=serializer.validated_data.get("pin")
        )
        account.refresh_from_db()
        return Response(DeveloperAccountSerializer(account).data)

    def _transition(self, request, transition):
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account = services.change_status(
            actor=request.user,
            account=self.get_object(),
            transition=transition,
            reason=serializer.validated_data["reason"],
        )
        account = self.get_queryset().get(pk=account.pk)
        return Response(DeveloperAccountSerializer(account).data)

    @extend_schema(request=StatusChangeSerializer, responses=DeveloperAccountSerializer)
    @action(detail=True, methods=["post"])
    def freeze(self, request, pk=None):
        """Block spending; deposits still allowed."""
        return self._transition(request, "freeze")

    @extend_schema(request=StatusChangeSerializer, responses=DeveloperAccountSerializer)
    @action(detail=True, methods=["post"])
    def unfreeze(self, request, pk=None):
        return self._transition(request, "unfreeze")

    @extend_schema(request=StatusChangeSerializer, responses=DeveloperAccountSerializer)
    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        """Only with a zero balance."""
        return self._transition(request, "close")

    @extend_schema(request=StatusChangeSerializer, responses=DeveloperAccountSerializer)
    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        return self._transition(request, "reopen")


class AccountTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AccountTransaction.objects.select_related("account__developer")
    serializer_class = AccountTransactionSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"list": ["finance.view"], "retrieve": ["finance.view"], "me": []}
    filterset_class = AccountTransactionFilter
    search_fields = ["description", "reference", "account__developer__full_name"]
    ordering_fields = ["created_at", "amount"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "me":
            qs = qs.filter(account__developer=_own_developer(self.request))
        return qs

    @action(detail=False, methods=["get"])
    def me(self, request):
        """The caller's own transactions."""
        return self.list(request)


def _money_response(txn, created):
    return Response(
        AccountTransactionSerializer(txn).data,
        status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
    )


class DepositView(APIView):
    permission_classes = [HasPermissions]
    required_permissions = {"post": ["finance.deposit"]}

    @extend_schema(
        request=DepositSerializer,
        responses={201: AccountTransactionSerializer, 200: AccountTransactionSerializer},
        parameters=[IDEMPOTENCY_PARAM],
        description="201 when posted; 200 with the original transaction when the "
        "Idempotency-Key was already used for this same deposit.",
    )
    def post(self, request):
        key = require_idempotency_key(request)
        serializer = DepositSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        txn, created = services.deposit(
            actor=request.user, idempotency_key=key, **serializer.validated_data
        )
        return _money_response(txn, created)


class AdjustmentView(APIView):
    permission_classes = [HasPermissions]
    required_permissions = {"post": ["finance.adjust"]}

    @extend_schema(
        request=AdjustmentSerializer,
        responses={201: AccountTransactionSerializer, 200: AccountTransactionSerializer},
        parameters=[IDEMPOTENCY_PARAM],
        description="Signed correction with a mandatory reason. Same idempotency rules "
        "as deposits.",
    )
    def post(self, request):
        key = require_idempotency_key(request)
        serializer = AdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        txn, created = services.adjust(
            actor=request.user, idempotency_key=key, **serializer.validated_data
        )
        return _money_response(txn, created)
