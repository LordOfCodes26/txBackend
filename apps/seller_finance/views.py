from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.rfid.scope import BuildingScopedMixin, sellers_within
from apps.sellers.access import CatalogPermission, SellerScopedQuerysetMixin
from apps.sellers.exceptions import SellerProfileNotFound
from apps.sellers.models import Seller
from common.idempotency import HEADER, require_idempotency_key
from common.permissions import HasPermissions

from . import services
from .filters import SellerPaymentFilter, SellerTransactionFilter
from .models import SellerAccount, SellerPayment, SellerTransaction
from .serializers import (
    PayoutRequestSerializer,
    PayReferenceSerializer,
    RejectSerializer,
    SellerAccountSerializer,
    SellerAdjustmentSerializer,
    SellerPaymentSerializer,
    SellerTransactionSerializer,
)

IDEMPOTENCY_PARAM = OpenApiParameter(
    HEADER,
    location=OpenApiParameter.HEADER,
    required=True,
    description="Unique per user action (use a UUID). Retries with the same key are safe.",
)


class SellerAccountViewSet(BuildingScopedMixin, viewsets.ReadOnlyModelViewSet):
    """`available_balance` = balance − `reserved` (open payouts)."""

    queryset = services.with_reserved(SellerAccount.objects.select_related("seller"))
    serializer_class = SellerAccountSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["seller_finance.view"],
        "retrieve": ["seller_finance.view"],
        "me": [],
    }
    filterset_fields = ["seller"]
    search_fields = ["seller__name"]

    def filter_by_buildings(self, qs, scope):
        return qs.filter(seller__in=sellers_within(scope))

    ordering_fields = ["balance", "seller__name", "updated_at"]

    @extend_schema(responses=SellerAccountSerializer)
    @action(detail=False, methods=["get"])
    def me(self, request):
        """The caller's own seller account, whatever the seller's status."""
        seller = Seller.objects.filter(user=request.user).first()
        if seller is None:
            raise SellerProfileNotFound()
        services.open_seller_account(seller)
        account = self.get_queryset().get(seller=seller)
        return Response(SellerAccountSerializer(account).data)


class SellerTransactionViewSet(
    BuildingScopedMixin, SellerScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet
):
    queryset = SellerTransaction.objects.select_related("account__seller")
    serializer_class = SellerTransactionSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {
        "list": ["seller_finance.view"],
        "retrieve": ["seller_finance.view"],
    }
    seller_actions = ("list", "retrieve")
    scope_permission = "seller_finance.view"
    seller_lookup = "account__seller"
    filterset_class = SellerTransactionFilter

    def filter_by_buildings(self, qs, scope):
        return qs.filter(account__seller__in=sellers_within(scope))

    search_fields = ["description", "reference"]
    ordering_fields = ["created_at", "amount"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.account.seller_id


class SellerPaymentViewSet(
    BuildingScopedMixin,
    SellerScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Payouts: REQUESTED → APPROVED → (PROCESSING →) PAID, or REJECTED / CANCELLED.

    Sellers request and cancel their own; finance staff approve, reject and pay.
    The requester can never approve their own payout.
    """

    queryset = SellerPayment.objects.select_related("seller")
    serializer_class = SellerPaymentSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {
        "list": ["seller_finance.view"],
        "retrieve": ["seller_finance.view"],
        "create": ["seller_finance.payout"],
        "cancel": ["seller_finance.payout"],
        "approve": ["seller_finance.payout"],
        "processing": ["seller_finance.payout"],
        "pay": ["seller_finance.payout"],
        "reject": ["seller_finance.payout"],
    }
    seller_actions = ("list", "retrieve", "create", "cancel")
    scope_permission = "seller_finance.view"
    filterset_class = SellerPaymentFilter

    def filter_by_buildings(self, qs, scope):
        return qs.filter(seller__in=sellers_within(scope))

    ordering_fields = ["created_at", "amount", "status"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.seller_id

    def _respond(self, payment, code=status.HTTP_200_OK):
        return Response(SellerPaymentSerializer(payment).data, status=code)

    @extend_schema(
        request=PayoutRequestSerializer,
        responses={201: SellerPaymentSerializer, 200: SellerPaymentSerializer},
        parameters=[IDEMPOTENCY_PARAM],
    )
    def create(self, request, *args, **kwargs):
        """Request a payout (up to the available balance)."""
        key = require_idempotency_key(request)
        serializer = PayoutRequestSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        payment, created = services.request_payout(
            actor=request.user, idempotency_key=key, **serializer.validated_data
        )
        return self._respond(payment, status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @extend_schema(request=None, responses=SellerPaymentSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        return self._respond(services.approve_payout(actor=request.user, payment=self.get_object()))

    @extend_schema(request=None, responses=SellerPaymentSerializer)
    @action(detail=True, methods=["post"])
    def processing(self, request, pk=None):
        """Optional step: the transfer has been started."""
        return self._respond(
            services.mark_processing(actor=request.user, payment=self.get_object())
        )

    @extend_schema(request=PayReferenceSerializer, responses=SellerPaymentSerializer)
    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        """The money has been handed over; debits the seller balance."""
        serializer = PayReferenceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = services.pay_payout(
            actor=request.user, payment=self.get_object(), **serializer.validated_data
        )
        return self._respond(payment)

    @extend_schema(request=RejectSerializer, responses=SellerPaymentSerializer)
    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        serializer = RejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = services.reject_payout(
            actor=request.user, payment=self.get_object(), **serializer.validated_data
        )
        return self._respond(payment)

    @extend_schema(request=None, responses=SellerPaymentSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Withdraw a request that has not been approved yet."""
        return self._respond(services.cancel_payout(actor=request.user, payment=self.get_object()))


class SellerAdjustmentView(APIView):
    permission_classes = [HasPermissions]
    required_permissions = {"post": ["seller_finance.adjust"]}

    @extend_schema(
        request=SellerAdjustmentSerializer,
        responses={201: SellerTransactionSerializer, 200: SellerTransactionSerializer},
        parameters=[IDEMPOTENCY_PARAM],
        description="Signed correction with a mandatory reason. A debit cannot use money "
        "reserved by open payouts.",
    )
    def post(self, request):
        key = require_idempotency_key(request)
        serializer = SellerAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        txn, created = services.adjust_seller(
            actor=request.user, idempotency_key=key, **serializer.validated_data
        )
        return Response(
            SellerTransactionSerializer(txn).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
