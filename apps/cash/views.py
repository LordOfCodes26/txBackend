from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.sellers.models import Seller
from apps.spreadsheets.xlsx import XLSX
from apps.stats.views import PeriodSerializer
from common.idempotency import HEADER, require_idempotency_key

from . import reports, services
from .access import sellers_for, visible_sellers
from .filters import CashEntryFilter
from .models import CashEntry
from .serializers import (
    CashEntrySerializer,
    CashOverviewSerializer,
    CashStoreDetailSerializer,
    HandoverSerializer,
    PriceChangeSerializer,
    ReasonSerializer,
)

IDEMPOTENCY_PARAM = OpenApiParameter(
    HEADER,
    location=OpenApiParameter.HEADER,
    required=True,
    description="Unique per action; a retry with the same key is safe.",
)


class CashPermission(IsAuthenticated):
    """`required_permissions` {action: codename}; None: anyone signed in, whose rows are
    narrowed (a store's own login sees its own store). Anything not listed is denied."""

    def has_permission(self, request, view) -> bool:
        if not super().has_permission(request, view):
            return False
        if request.method == "OPTIONS":
            return True
        if view.action not in view.required_permissions:
            return False
        permission = view.required_permissions[view.action]
        return permission is None or request.user.has_rbac_perm(permission)


def _ensure_sees_cash(user) -> None:
    if not visible_sellers(user).exists() and not user.has_rbac_perm("cash.view"):
        raise PermissionDenied()


def _checked(serializer_class, request, *, data=None, **context):
    serializer = serializer_class(data=request.data if data is None else data, context=context)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class CashStoreViewSet(viewsets.GenericViewSet):
    """Cash control: per store, the cash sales, cash returned, handovers and what it
    still owes. A store's own login sees its own store."""

    permission_classes = [CashPermission]
    required_permissions = {
        "list": None,
        "retrieve": None,
        "price_changes": None,
        "export": None,
    }
    queryset = CashEntry.objects.none()
    pagination_class = None

    def _period(self):
        data = _checked(PeriodSerializer, self.request, data=self.request.query_params)
        return data["date_from"], data["date_to"]

    @extend_schema(parameters=[PeriodSerializer], responses=CashOverviewSerializer)
    def list(self, request):
        _ensure_sees_cash(request.user)
        rows = reports.stores(visible_sellers(request.user), *self._period())
        body = {"currency": settings.CURRENCY, "stores": rows}
        return Response(CashOverviewSerializer(body).data)

    @extend_schema(
        parameters=[PeriodSerializer, OpenApiParameter("id", int, OpenApiParameter.PATH)],
        responses=CashStoreDetailSerializer,
    )
    def retrieve(self, request, pk=None):
        seller = get_object_or_404(visible_sellers(request.user), pk=pk)
        first, last = self._period()
        rows = reports.stores(visible_sellers(request.user).filter(pk=seller.pk), first, last)
        if not rows:
            raise NotFound()  # it never took cash
        body = {
            **rows[0],
            "currency": settings.CURRENCY,
            "daily": reports.daily(seller, first, last),
        }
        return Response(CashStoreDetailSerializer(body).data)

    @extend_schema(
        parameters=[PeriodSerializer, OpenApiParameter("id", int, OpenApiParameter.PATH)],
        responses=PriceChangeSerializer(many=True),
    )
    @action(detail=True, methods=["get"], url_path="price-changes")
    def price_changes(self, request, pk=None):
        """The store's prices changed and goods added in the period, newest first."""
        seller = get_object_or_404(visible_sellers(request.user), pk=pk)
        first, last = self._period()
        rows = reports.price_changes(Seller.objects.filter(pk=seller.pk), first, last)
        return Response(PriceChangeSerializer(rows, many=True).data)

    @extend_schema(parameters=[PeriodSerializer], responses={(200, XLSX): OpenApiTypes.BINARY})
    @action(detail=False, methods=["get"])
    def export(self, request):
        """The period as .xlsx: the stores, their days, every entry and the price changes."""
        from apps.spreadsheets.exports import cash_workbook

        _ensure_sees_cash(request.user)
        if not request.user.has_rbac_perm("excel.export"):
            raise PermissionDenied()
        first, last = self._period()
        book = cash_workbook(visible_sellers(request.user), first, last)
        response = HttpResponse(book, content_type=XLSX)
        response["Content-Disposition"] = f'attachment; filename="cash-{first}-to-{last}.xlsx"'
        return response


class CashEntryViewSet(viewsets.ReadOnlyModelViewSet):
    """The stores' cash ledger. `handover/` records cash finance took from a store."""

    queryset = CashEntry.objects.select_related("seller", "actor", "reversal")
    serializer_class = CashEntrySerializer
    permission_classes = [CashPermission]
    required_permissions = {
        "list": None,
        "retrieve": None,
        "handover": "cash.handover",
        "reverse": "cash.handover",
    }
    filterset_class = CashEntryFilter
    search_fields = ["note", "reference", "seller__name"]
    ordering_fields = ["created_at", "amount"]

    def get_queryset(self):
        return super().get_queryset().filter(seller__in=visible_sellers(self.request.user))

    def _response(self, entry, created):
        entry = CashEntry.objects.select_related("seller", "actor", "reversal").get(pk=entry.pk)
        return Response(
            CashEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @extend_schema(
        request=HandoverSerializer,
        responses={201: CashEntrySerializer, 200: CashEntrySerializer},
        parameters=[IDEMPOTENCY_PARAM],
    )
    @action(detail=False, methods=["post"])
    def handover(self, request):
        """Cash counted and taken from a store. More than it owes needs a note. Not for a
        store one works for (403 OWN_STORE)."""
        key = require_idempotency_key(request)
        data = _checked(
            HandoverSerializer, request, sellers=sellers_for(request.user, "cash.handover")
        )
        return self._response(*services.hand_over(actor=request.user, idempotency_key=key, **data))

    @extend_schema(
        request=ReasonSerializer,
        responses={201: CashEntrySerializer, 200: CashEntrySerializer},
        parameters=[IDEMPOTENCY_PARAM],
    )
    @action(detail=True, methods=["post"])
    def reverse(self, request, pk=None):
        """Cancel a handover entered by mistake (409 NOT_REVERSIBLE otherwise)."""
        key = require_idempotency_key(request)
        entry = get_object_or_404(
            CashEntry, pk=pk, seller__in=sellers_for(request.user, "cash.handover")
        )
        data = _checked(ReasonSerializer, request)
        return self._response(
            *services.reverse_entry(actor=request.user, entry=entry, idempotency_key=key, **data)
        )
