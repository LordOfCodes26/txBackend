from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.developers.exceptions import DeveloperProfileNotFound
from apps.developers.models import Developer
from apps.sellers.access import CatalogPermission, SellerScopedQuerysetMixin
from common.idempotency import HEADER, require_idempotency_key
from common.middleware import client_ip

from . import services
from .filters import PurchaseFilter
from .models import Purchase
from .serializers import (
    ConfirmSerializer,
    DetectedReaderSerializer,
    ItemAddSerializer,
    ItemUpdateSerializer,
    PurchaseCreateSerializer,
    PurchaseSerializer,
    SetReaderSerializer,
    TillReaderSerializer,
)

SELLER_ACTIONS = (
    "list",
    "retrieve",
    "create",
    "add_item",
    "item",
    "set_reader",
    "readers",
    "detected_reader",
    "confirm",
    "cancel",
)


class PurchaseViewSet(
    SellerScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Till flow: create a draft, add items, confirm with the developer's card + PIN.

    Active sellers work with their own purchases; `purchase.*` permissions cover all.
    Developers see their own confirmed purchases at `/purchases/me/`.
    """

    queryset = Purchase.objects.select_related(
        "seller",
        "service_position",
        "developer",
        "card",
        "account_transaction",
        "presented_event__developer",
        "reader",
    ).prefetch_related("items__good__rental")
    serializer_class = PurchaseSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {
        "list": ["purchase.view"],
        "retrieve": ["purchase.view"],
        "create": ["purchase.create"],
        "add_item": ["purchase.create"],
        "item": ["purchase.create"],
        "set_reader": ["purchase.create"],
        "readers": ["purchase.create"],
        "detected_reader": ["purchase.create"],
        "confirm": ["purchase.confirm"],
        "cancel": ["purchase.cancel"],
        "me": [],
    }
    seller_actions = SELLER_ACTIONS
    scope_permission = "purchase.view"
    filterset_class = PurchaseFilter
    search_fields = ["developer__full_name", "developer__employee_number", "seller__name"]
    ordering_fields = ["created_at", "confirmed_at", "total"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.seller_id

    def get_queryset(self):
        if self.action == "me":
            developer = Developer.objects.filter(user=self.request.user).first()
            if developer is None:
                raise DeveloperProfileNotFound()
            return self.queryset.filter(developer=developer)
        return super().get_queryset()

    def _respond(self, purchase, code=status.HTTP_200_OK):
        purchase = self.queryset.get(pk=purchase.pk)
        return Response(PurchaseSerializer(purchase).data, status=code)

    @extend_schema(request=PurchaseCreateSerializer, responses={201: PurchaseSerializer})
    def create(self, request, *args, **kwargs):
        serializer = PurchaseCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        purchase = services.create_purchase(
            actor=request.user, client_ip=client_ip(request), **serializer.validated_data
        )
        return self._respond(purchase, status.HTTP_201_CREATED)

    @extend_schema(responses=DetectedReaderSerializer)
    @action(detail=False, methods=["get"], url_path="detected-reader")
    def detected_reader(self, request):
        """Till readers connected to the caller's PC (same network address, heard from in the
        last 2 minutes). `reader` is set when exactly one is connected; `candidates` is empty
        when none is."""
        ip = client_ip(request)
        candidates = list(services.readers_at(ip)[:5])
        return Response(
            DetectedReaderSerializer(
                {
                    "ip": ip,
                    "reader": candidates[0] if len(candidates) == 1 else None,
                    "candidates": candidates,
                }
            ).data
        )

    @extend_schema(responses=TillReaderSerializer(many=True))
    @action(detail=False, methods=["get"])
    def readers(self, request):
        """Till readers to choose from ("which reader is plugged into this PC?")."""
        from .serializers import _readers

        return Response(TillReaderSerializer(_readers().order_by("code"), many=True).data)

    @extend_schema(request=SetReaderSerializer, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"], url_path="reader")
    def set_reader(self, request, pk=None):
        """Use another till reader for this draft (the reader moved to another PC)."""
        purchase = self.get_object()
        serializer = SetReaderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.set_reader(purchase=purchase, reader=serializer.validated_data["reader"])
        return self._respond(purchase)

    @extend_schema(request=ItemAddSerializer, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"], url_path="items")
    def add_item(self, request, pk=None):
        """Add a good (or increase its quantity). A rental (court) is added the same way
        with `start` = its first slot and `quantity` = number of slots. Returns the whole
        purchase."""
        purchase = self.get_object()
        serializer = ItemAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.add_item(purchase=purchase, **serializer.validated_data)
        return self._respond(purchase)

    @extend_schema(methods=["PATCH"], request=ItemUpdateSerializer, responses=PurchaseSerializer)
    @extend_schema(methods=["DELETE"], request=None, responses=PurchaseSerializer)
    @action(detail=True, methods=["patch", "delete"], url_path=r"items/(?P<item_id>\d+)")
    def item(self, request, pk=None, item_id=None):
        """PATCH changes the quantity (for a booking: the number of slots); DELETE removes
        the item. Returns the whole purchase."""
        purchase = self.get_object()
        item = get_object_or_404(purchase.items, pk=item_id)
        if request.method == "DELETE":
            services.remove_item(purchase=purchase, item=item)
        else:
            serializer = ItemUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            services.update_item(purchase=purchase, item=item, **serializer.validated_data)
        return self._respond(purchase)

    @extend_schema(
        request=ConfirmSerializer,
        responses={201: PurchaseSerializer, 200: PurchaseSerializer},
        parameters=[
            OpenApiParameter(
                HEADER,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Unique per checkout attempt; a retry with the same key is safe.",
            )
        ],
        description="Charge the card holder. 201 when confirmed now; 200 when this "
        "Idempotency-Key already confirmed it.",
    )
    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        key = require_idempotency_key(request)
        purchase = self.get_object()
        serializer = ConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        purchase, created = services.confirm_purchase(
            actor=request.user,
            purchase=purchase,
            idempotency_key=key,
            **serializer.validated_data,
        )
        return self._respond(purchase, status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @extend_schema(request=None, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Abandon a draft. Confirmed purchases are final."""
        purchase = services.cancel_purchase(actor=request.user, purchase=self.get_object())
        return self._respond(purchase)

    @action(detail=False, methods=["get"])
    def me(self, request):
        """The caller's own purchases (as a developer)."""
        return self.list(request)
