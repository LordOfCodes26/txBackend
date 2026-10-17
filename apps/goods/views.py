from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.rfid.scope import BuildingScopedMixin
from apps.sellers.access import (
    GOODS,
    CatalogPermission,
    SellerScopedQuerysetMixin,
    acting_seller,
    seller_areas,
    seller_kinds,
    works_playground,
)
from apps.spreadsheets.xlsx import XLSX
from apps.stats.views import PeriodSerializer

from . import services
from .filters import GoodFilter, InventoryMovementFilter
from .models import CourtPrice, Good, GoodCategory, GoodKind, InventoryMovement
from .serializers import (
    CourtPriceSerializer,
    GoodCategorySerializer,
    GoodImageSerializer,
    GoodSerializer,
    GoodStatisticsQuerySerializer,
    GoodStatisticsSerializer,
    InventoryMovementSerializer,
    StockChangeSerializer,
)

ALL_GOOD_ACTIONS = (
    "list",
    "retrieve",
    "create",
    "partial_update",
    "destroy",
    "stock",
    "upload_image",
    "delete_image",
    "statistics",
    "statistics_export",
    "prices",
    "price",
)


def goods_sold(goods, after, before) -> list[dict]:
    """Per good of `goods`: units sold, paid sales and money taken in [after, before) (either
    may be None), and the stock now; the most money first."""
    from apps.purchases.models import PurchaseItem, PurchaseKind, PurchaseStatus

    items = PurchaseItem.objects.filter(
        good__in=goods,
        purchase__status=PurchaseStatus.CONFIRMED,
        purchase__kind=PurchaseKind.SALE,
    )
    if after is not None:
        items = items.filter(purchase__confirmed_at__gte=after)
    if before is not None:
        items = items.filter(purchase__confirmed_at__lt=before)
    totals = {
        row["good"]: row
        for row in items.values("good").annotate(
            sold=Sum("quantity"),
            sales=Count("purchase", distinct=True),
            money=Sum("line_total"),
        )
    }
    zero = Decimal("0.00")
    rows = []
    for good in goods:
        sold = totals.get(good.pk, {})
        rows.append(
            {
                "good": good.pk,
                "name": good.name,
                "kind": good.kind,
                "category_name": good.category.name if good.category_id else None,
                "seller_name": good.service_position.seller.name,
                "service_position_name": good.service_position.name,
                "price": good.price,
                "sold": sold.get("sold") or 0,
                "sales": sold.get("sales") or 0,
                "money": sold.get("money") or zero,
                "stock": good.quantity if good.track_stock else None,
                "currency": settings.CURRENCY,
            }
        )
    rows.sort(key=lambda row: (-row["money"], -row["sold"], row["name"]))
    return rows


class GoodViewSet(BuildingScopedMixin, SellerScopedQuerysetMixin, viewsets.ModelViewSet):
    """Seller managers manage every good; an active seller manages their own.

    DELETE is a soft delete. Stock changes only through `POST /goods/{id}/stock/`.
    """

    queryset = Good.objects.select_related(
        "service_position__seller", "rental", "category"
    ).prefetch_related("images", "court_prices")
    serializer_class = GoodSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {
        "list": ["good.view"],
        "retrieve": ["good.view"],
        "create": ["good.create"],
        "partial_update": ["good.update"],
        "destroy": ["good.delete"],
        "stock": ["good.stock"],
        "upload_image": ["good.update"],
        "delete_image": ["good.update"],
        "statistics": ["good.view", "purchase.view"],
        "statistics_export": ["good.view", "purchase.view"],
        "prices": ["good.update"],
        "price": ["good.update"],
    }
    seller_actions = ALL_GOOD_ACTIONS
    scope_permission = "good.view"
    seller_lookup = "service_position__seller"
    position_lookup = "service_position"
    building_lookup = "service_position__building"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    filterset_class = GoodFilter
    search_fields = ["name", "description", "sku", "service_position__seller__name"]
    ordering_fields = ["name", "price", "quantity", "created_at"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.service_position.seller_id

    @staticmethod
    def owner_position_id(obj):
        return obj.service_position_id

    # Courts are the shared playground's: every playground seller works with all of them.
    playground_filter = Q(kind=GoodKind.RENTAL)

    def filter_seller_area(self, qs, user):
        # Goods sellers see their store's goods (playground sellers get every court above).
        return qs.filter(kind__in=seller_kinds(user))

    def unpinned(self, user) -> bool:
        """Playground sellers add and change courts at any counter (no store of their own)."""
        if not works_playground(user):
            return False
        if self.action == "create":
            return self.request.data.get("kind") == GoodKind.RENTAL
        pk = self.kwargs.get("pk")
        return pk is not None and Good.objects.filter(pk=pk, kind=GoodKind.RENTAL).exists()

    def _check_court(self, kind) -> None:
        """Courts need `court.manage`, or a playground seller's own store. Other goods need
        the action's permission, or a goods seller's own store."""
        user = self.request.user
        if kind == GoodKind.RENTAL:
            if user.has_rbac_perm("court.manage"):
                return
            if not works_playground(user):
                raise PermissionDenied(_("Your account cannot add or change courts."))
            return
        if user.has_rbac_perms(self.required_permissions.get(self.action, [])):
            return
        if GOODS not in seller_areas(user):
            raise PermissionDenied(_("Your account cannot add or change goods."))

    def perform_create(self, serializer):
        self._check_court(serializer.validated_data.get("kind", GoodKind.PRODUCT))
        serializer.instance = services.create_good(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        self._check_court(serializer.instance.kind)
        if "kind" in serializer.validated_data:  # turning a good into a court, or back
            self._check_court(serializer.validated_data["kind"])
        serializer.instance = services.update_good(
            actor=self.request.user, good=serializer.instance, **serializer.validated_data
        )

    def perform_destroy(self, instance):
        self._check_court(instance.kind)
        services.delete_good(actor=self.request.user, good=instance)

    def _court(self) -> Good:
        good = self.get_object()
        if good.kind != GoodKind.RENTAL:
            raise PermissionDenied(_("Only courts have a price list."))
        self._check_court(GoodKind.RENTAL)
        return good

    @extend_schema(request=CourtPriceSerializer, responses={201: CourtPriceSerializer})
    @action(detail=True, methods=["post"])
    def prices(self, request, pk=None):
        """Add a price to a court, e.g. "Per person" 5 per person per slot."""
        good = self._court()
        serializer = CourtPriceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = {k: v for k, v in serializer.validated_data.items() if k != "is_active"}
        row = services.add_court_price(actor=request.user, good=good, **data)
        return Response(CourtPriceSerializer(row).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=CourtPriceSerializer, responses={200: CourtPriceSerializer, 204: None})
    @action(detail=True, methods=["patch", "delete"], url_path=r"prices/(?P<price_id>\d+)")
    def price(self, request, pk=None, price_id=None):
        """Change one of the court's prices, or remove it (switched off if it was used)."""
        good = self._court()
        row = get_object_or_404(CourtPrice, pk=price_id, good=good)
        if request.method == "DELETE":
            services.remove_court_price(actor=request.user, row=row)
            return Response(status=status.HTTP_204_NO_CONTENT)
        serializer = CourtPriceSerializer(row, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        row = services.update_court_price(actor=request.user, row=row, **serializer.validated_data)
        return Response(CourtPriceSerializer(row).data)

    @extend_schema(
        parameters=[GoodStatisticsQuerySerializer],
        responses=GoodStatisticsSerializer(many=True),
    )
    @action(detail=False, methods=["get"], pagination_class=None)
    def statistics(self, request):
        """Per good (not courts): units sold, paid purchases and money taken in the period
        (`confirmed_after` / `confirmed_before`), and the stock now. The goods are those of
        the list (a seller: its own store's goods), with the list's filters."""
        query = GoodStatisticsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        rows = goods_sold(
            self._sales_goods(),
            query.validated_data.get("confirmed_after"),
            query.validated_data.get("confirmed_before"),
        )
        return Response(GoodStatisticsSerializer(rows, many=True).data)

    def _sales_goods(self):
        return (
            self.filter_queryset(self.get_queryset())
            .exclude(kind=GoodKind.RENTAL)
            .select_related("service_position__seller", "category")
            .order_by()
        )

    @extend_schema(parameters=[PeriodSerializer], responses={(200, XLSX): OpenApiTypes.BINARY})
    @action(detail=False, methods=["get"], url_path="statistics/export")
    def statistics_export(self, request):
        """The Sales report as .xlsx for `date_from`..`date_to` (company days): a Summary
        sheet (money taken, paid sales, refunds, goods sold) and a Goods sold sheet."""
        from apps.spreadsheets.exports import sales_report_workbook
        from apps.stats.services import _local_bounds

        period = PeriodSerializer(data=request.query_params)
        period.is_valid(raise_exception=True)
        first, last = period.validated_data["date_from"], period.validated_data["date_to"]
        after, before = _local_bounds(first, last)
        goods = self._sales_goods()
        rows = [row for row in goods_sold(goods, after, before) if row["sold"] > 0]
        book = sales_report_workbook(goods, rows, after, before, first, last)
        response = HttpResponse(book, content_type=XLSX)
        response["Content-Disposition"] = f'attachment; filename="sales-{first}-to-{last}.xlsx"'
        return response

    def _good_response(self, good, code=status.HTTP_200_OK):
        good = self.get_queryset().get(pk=good.pk)
        return Response(GoodSerializer(good, context=self.get_serializer_context()).data, code)

    @extend_schema(request=StockChangeSerializer, responses={201: InventoryMovementSerializer})
    @action(detail=True, methods=["post"], parser_classes=[JSONParser])
    def stock(self, request, pk=None):
        """RESTOCK (+quantity), DAMAGE (-quantity, reason) or ADJUSTMENT (counted_quantity,
        reason). Returns the recorded movement."""
        good = self.get_object()
        serializer = StockChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        movement = services.adjust_stock(actor=request.user, good=good, **serializer.validated_data)
        return Response(InventoryMovementSerializer(movement).data, status.HTTP_201_CREATED)

    @extend_schema(
        request={"multipart/form-data": GoodImageSerializer}, responses={201: GoodSerializer}
    )
    @action(
        detail=True,
        methods=["post"],
        url_path="images",
        parser_classes=[MultiPartParser, FormParser],
    )
    def upload_image(self, request, pk=None):
        """Upload one image (multipart field `image`). Returns the updated good."""
        good = self.get_object()
        serializer = GoodImageSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.add_image(actor=request.user, good=good, **serializer.validated_data)
        return self._good_response(good, status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={204: None})
    @action(detail=True, methods=["delete"], url_path=r"images/(?P<image_id>\d+)")
    def delete_image(self, request, pk=None, image_id=None):
        good = self.get_object()
        image = get_object_or_404(good.images, pk=image_id)
        services.remove_image(actor=request.user, image=image)
        return Response(status=status.HTTP_204_NO_CONTENT)


class InventoryMovementViewSet(
    BuildingScopedMixin, SellerScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet
):
    queryset = InventoryMovement.objects.select_related("good")
    serializer_class = InventoryMovementSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {
        "list": ["good.view"],
        "retrieve": ["good.view"],
        "summary": ["good.view"],
    }
    seller_actions = ("list", "retrieve", "summary")
    seller_area = GOODS  # stock: courts have none
    scope_permission = "good.view"
    seller_lookup = "good__service_position__seller"
    position_lookup = "good__service_position"
    building_lookup = "good__service_position__building"
    filterset_class = InventoryMovementFilter
    ordering_fields = ["created_at"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.good.service_position.seller_id

    @staticmethod
    def owner_position_id(obj):
        return obj.good.service_position_id

    @extend_schema(
        parameters=[
            OpenApiParameter("created_after", OpenApiTypes.DATETIME, required=True),
            OpenApiParameter("created_before", OpenApiTypes.DATETIME, required=True),
            OpenApiParameter("seller", int, required=False),
            OpenApiParameter("category", int, required=False),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    @action(detail=False, methods=["get"], pagination_class=None)
    def summary(self, request):
        """Each good that moved in the range [`created_after`, `created_before`): its stock at
        the start, what came in (restock, initial stock, returns), went out (sales, damage),
        stock counts (+/-), and its stock at the end."""
        from django.utils.dateparse import parse_datetime

        after = parse_datetime(request.query_params.get("created_after", ""))
        before = parse_datetime(request.query_params.get("created_before", ""))
        if after is None or before is None or before <= after:
            raise ValidationError(
                {"created_after": [_("Give a range: created_after < created_before.")]}
            )
        base = self.get_queryset()
        for param, lookup in (
            ("seller", "good__service_position__seller"),
            ("category", "good__category"),
            ("good", "good"),
        ):
            value = request.query_params.get(param, "")
            if value.isdigit():
                base = base.filter(**{lookup: int(value)})
        sums = (
            base.filter(created_at__gte=after, created_at__lt=before)
            .values("good", "kind")
            .annotate(total=Sum("quantity_delta"))
        )
        moved: dict[int, dict[str, int]] = {}
        for row in sums:
            moved.setdefault(row["good"], {})[row["kind"]] = row["total"]
        start = dict(
            base.filter(good__in=moved, created_at__lt=after)
            .order_by("good", "-created_at", "-id")
            .distinct("good")
            .values_list("good", "quantity_after")
        )
        goods = Good.all_objects.filter(pk__in=moved).select_related(
            "service_position__seller", "category"
        )
        rows = []
        for good in sorted(goods, key=lambda g: g.name.lower()):
            kinds = moved[good.pk]
            begin = start.get(good.pk, 0)
            rows.append(
                {
                    "good": good.pk,
                    "name": good.name,
                    "category_name": good.category.name if good.category_id else None,
                    "seller_name": good.service_position.seller.name,
                    "service_position_name": good.service_position.name,
                    "start": begin,
                    "added": kinds.get("RESTOCK", 0) + kinds.get("INITIAL_STOCK", 0),
                    "returned": kinds.get("RETURN", 0),
                    "sold": -kinds.get("SALE", 0),
                    "damaged": -kinds.get("DAMAGE", 0),
                    "counted": kinds.get("ADJUSTMENT", 0),
                    "end": begin + sum(kinds.values()),
                    "stock_now": good.quantity if good.track_stock else None,
                }
            )
        return Response(rows)


class GoodCategoryViewSet(viewsets.ModelViewSet):
    """A store's categories of goods. Staff with goods rights see and change every store's
    (`?seller=` narrows it); a goods store's own login (owner or counter manager) its own."""

    serializer_class = GoodCategorySerializer
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filterset_fields = ["seller"]
    search_fields = ["name"]
    ordering_fields = ["position", "name"]
    pagination_class = None

    def _store(self):
        """None: staff (every store). Else the user's own goods store; no store: denied."""
        user = self.request.user
        need = "good.view" if self.request.method in ("GET", "HEAD", "OPTIONS") else "good.update"
        if user.has_rbac_perm(need):
            return None
        seller = acting_seller(user)
        if seller is None or GOODS not in seller_areas(user):
            raise PermissionDenied(_("Your account cannot manage goods categories."))
        return seller

    def get_queryset(self):
        rows = GoodCategory.objects.select_related("seller").annotate(
            goods_count=Count("goods", filter=Q(goods__deleted_at__isnull=True))
        )
        store = self._store()
        return rows if store is None else rows.filter(seller=store)

    def perform_create(self, serializer):
        store = self._store()
        seller = store or serializer.validated_data.get("seller")
        if seller is None:
            raise ValidationError({"seller": [_("This field is required.")]})
        data = {k: v for k, v in serializer.validated_data.items() if k != "seller"}
        if GoodCategory.objects.filter(seller=seller, name__iexact=data["name"]).exists():
            raise ValidationError({"name": [_("This store already has this category.")]})
        serializer.instance = services.create_category(
            actor=self.request.user, seller=seller, **data
        )

    def perform_update(self, serializer):
        data = {k: v for k, v in serializer.validated_data.items() if k != "seller"}
        name = data.get("name")
        row = serializer.instance
        if name and (
            GoodCategory.objects.filter(seller=row.seller, name__iexact=name)
            .exclude(pk=row.pk)
            .exists()
        ):
            raise ValidationError({"name": [_("This store already has this category.")]})
        serializer.instance = services.update_category(actor=self.request.user, row=row, **data)

    def perform_destroy(self, instance):
        services.delete_category(actor=self.request.user, row=instance)
