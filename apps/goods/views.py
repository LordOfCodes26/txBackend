from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.rfid.scope import BuildingScopedMixin
from apps.sellers.access import (
    GOODS,
    PLAYGROUND,
    CatalogPermission,
    SellerScopedQuerysetMixin,
    acting_seller,
    seller_areas,
    seller_kinds,
)

from . import services
from .filters import GoodFilter, InventoryMovementFilter
from .models import Good, GoodKind, InventoryMovement
from .serializers import (
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
)


class GoodViewSet(BuildingScopedMixin, SellerScopedQuerysetMixin, viewsets.ModelViewSet):
    """Seller managers manage every good; an active seller manages their own.

    DELETE is a soft delete. Stock changes only through `POST /goods/{id}/stock/`.
    """

    queryset = Good.objects.select_related("service_position__seller", "rental").prefetch_related(
        "images"
    )
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

    def filter_seller_area(self, qs, user):
        # Playground sellers see their courts, goods sellers the rest of their goods.
        return qs.filter(kind__in=seller_kinds(user))

    def _check_court(self, kind) -> None:
        """Courts need `court.manage`, or a playground seller's own store. Other goods need
        the action's permission, or a goods seller's own store."""
        user = self.request.user
        if kind == GoodKind.RENTAL:
            if user.has_rbac_perm("court.manage"):
                return
            if acting_seller(user) is None or PLAYGROUND not in seller_areas(user):
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

    @extend_schema(
        parameters=[GoodStatisticsQuerySerializer],
        responses=GoodStatisticsSerializer(many=True),
    )
    @action(detail=False, methods=["get"], pagination_class=None)
    def statistics(self, request):
        """Per good (not courts): units sold, paid purchases and money taken in the period
        (`confirmed_after` / `confirmed_before`), and the stock now. The goods are those of
        the list (a seller: its own store's goods), with the list's filters."""
        from apps.purchases.models import PurchaseItem, PurchaseKind, PurchaseStatus

        query = GoodStatisticsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        goods = (
            self.filter_queryset(self.get_queryset())
            .exclude(kind=GoodKind.RENTAL)
            .select_related("service_position__seller")
            .order_by()
        )
        items = PurchaseItem.objects.filter(
            good__in=goods,
            purchase__status=PurchaseStatus.CONFIRMED,
            purchase__kind=PurchaseKind.SALE,
        )
        if "confirmed_after" in query.validated_data:
            items = items.filter(
                purchase__confirmed_at__gte=query.validated_data["confirmed_after"]
            )
        if "confirmed_before" in query.validated_data:
            items = items.filter(
                purchase__confirmed_at__lt=query.validated_data["confirmed_before"]
            )
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
        return Response(GoodStatisticsSerializer(rows, many=True).data)

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
    required_permissions = {"list": ["good.view"], "retrieve": ["good.view"]}
    seller_actions = ("list", "retrieve")
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
