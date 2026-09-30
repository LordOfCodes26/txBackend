from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.sellers.access import CatalogPermission, SellerScopedQuerysetMixin

from . import services
from .filters import GoodFilter, InventoryMovementFilter
from .models import Good, InventoryMovement
from .serializers import (
    GoodImageSerializer,
    GoodSerializer,
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
)


class GoodViewSet(SellerScopedQuerysetMixin, viewsets.ModelViewSet):
    """Seller managers manage every good; an active seller manages their own.

    DELETE is a soft delete. Stock changes only through `POST /goods/{id}/stock/`.
    """

    queryset = Good.objects.select_related("service_position__seller").prefetch_related("images")
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
    }
    seller_actions = ALL_GOOD_ACTIONS
    scope_permission = "good.view"
    seller_lookup = "service_position__seller"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    filterset_class = GoodFilter
    search_fields = ["name", "description", "sku", "service_position__seller__name"]
    ordering_fields = ["name", "price", "quantity", "created_at"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.service_position.seller_id

    def perform_create(self, serializer):
        serializer.instance = services.create_good(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        serializer.instance = services.update_good(
            actor=self.request.user, good=serializer.instance, **serializer.validated_data
        )

    def perform_destroy(self, instance):
        services.delete_good(actor=self.request.user, good=instance)

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


class InventoryMovementViewSet(SellerScopedQuerysetMixin, viewsets.ReadOnlyModelViewSet):
    queryset = InventoryMovement.objects.select_related("good")
    serializer_class = InventoryMovementSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {"list": ["good.view"], "retrieve": ["good.view"]}
    seller_actions = ("list", "retrieve")
    scope_permission = "good.view"
    seller_lookup = "good__service_position__seller"
    filterset_class = InventoryMovementFilter
    ordering_fields = ["created_at"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.good.service_position.seller_id
