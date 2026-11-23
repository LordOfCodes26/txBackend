from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.sellers.models import Seller
from apps.spreadsheets.xlsx import XLSX
from apps.stats.views import PeriodSerializer

from . import reports
from .access import visible_sellers
from .serializers import (
    CashOverviewSerializer,
    CashStoreDetailSerializer,
    PriceChangeSerializer,
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
    """What each store sold by card (finance pays it to the store) and in cash (the seller has
    it). A store's own login sees its own store."""

    permission_classes = [CashPermission]
    required_permissions = {
        "list": None,
        "retrieve": None,
        "price_changes": None,
        "export": None,
    }
    queryset = Seller.objects.none()
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
            raise NotFound()  # closed, and sold nothing
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
        """The period as .xlsx: the stores, their days and the price changes."""
        from apps.spreadsheets.exports import cash_workbook

        _ensure_sees_cash(request.user)
        if not request.user.has_rbac_perm("excel.export"):
            raise PermissionDenied()
        first, last = self._period()
        book = cash_workbook(visible_sellers(request.user), first, last)
        response = HttpResponse(book, content_type=XLSX)
        response["Content-Disposition"] = f'attachment; filename="cash-{first}-to-{last}.xlsx"'
        return response
