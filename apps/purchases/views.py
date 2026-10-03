from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.developers.exceptions import DeveloperProfileNotFound
from apps.developers.models import Developer
from apps.rfid.scope import BuildingScopedMixin
from apps.sellers.access import CatalogPermission, SellerScopedQuerysetMixin
from common.idempotency import HEADER, require_idempotency_key
from common.middleware import client_ip

from . import services
from .filters import PurchaseFilter
from .models import Purchase, PurchaseKind, PurchaseStatus
from .serializers import (
    ConfirmSerializer,
    ItemAddSerializer,
    ItemUpdateSerializer,
    PerformanceSerializer,
    PurchaseCreateSerializer,
    PurchaseSerializer,
    ReadersQuerySerializer,
    SetReaderSerializer,
    TillReaderSerializer,
)

ZERO = Decimal("0.00")
PERFORMANCE_FIGURES = ("sales_count", "sales_total", "bookings_count", "bookings_total", "total")

SELLER_ACTIONS = (
    "list",
    "performance",
    "retrieve",
    "create",
    "add_item",
    "item",
    "set_reader",
    "readers",
    "wait",
    "stop_waiting",
    "confirm",
    "cancel",
)


class PurchaseViewSet(
    BuildingScopedMixin,
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
        "performance": ["purchase.view"],
        "create": ["purchase.create"],
        "add_item": ["purchase.create"],
        "item": ["purchase.create"],
        "set_reader": ["purchase.create"],
        "readers": ["purchase.create"],
        "wait": ["purchase.create"],
        "stop_waiting": ["purchase.create"],
        "confirm": ["purchase.confirm"],
        "cancel": ["purchase.cancel"],
        "me": [],
    }
    seller_actions = SELLER_ACTIONS
    scope_permission = "purchase.view"
    filterset_class = PurchaseFilter
    search_fields = ["developer__full_name", "developer__employee_number", "seller__name"]
    ordering_fields = ["created_at", "confirmed_at", "total"]

    position_lookup = "service_position"
    building_lookup = "service_position__building"

    @staticmethod
    def owner_seller_id(obj):
        return obj.seller_id

    @staticmethod
    def owner_position_id(obj):
        return obj.service_position_id

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

    @extend_schema(responses=PerformanceSerializer(many=True))
    @action(detail=False, methods=["get"])
    def performance(self, request):
        """Sales per sell position: confirmed till sales and court bookings, counts and
        totals. Use the list filters, e.g. `?confirmed_after=2026-10-01T00:00:00Z&
        confirmed_before=...` and `seller`, `service_position`. Narrowed like the list
        (own seller / position, or a building manager's buildings)."""
        rows = (
            self.filter_queryset(self.get_queryset())
            .filter(status=PurchaseStatus.CONFIRMED)
            .order_by()
            .values(
                "service_position",
                "service_position__name",
                "seller",
                "seller__name",
                "service_position__building",
                "service_position__building__name",
            )
            .annotate(
                sales_count=Count("pk", filter=Q(kind=PurchaseKind.SALE)),
                sales_total=Sum("total", filter=Q(kind=PurchaseKind.SALE), default=ZERO),
                bookings_count=Count("pk", filter=Q(kind=PurchaseKind.BOOKING)),
                bookings_total=Sum("total", filter=Q(kind=PurchaseKind.BOOKING), default=ZERO),
                total=Sum("total", default=ZERO),
            )
            .order_by("-total", "service_position")
        )
        data = [
            {
                "service_position": r["service_position"],
                "service_position_name": r["service_position__name"],
                "seller": r["seller"],
                "seller_name": r["seller__name"],
                "building": r["service_position__building"],
                "building_name": r["service_position__building__name"],
                **{k: r[k] for k in PERFORMANCE_FIGURES},
            }
            for r in rows
        ]
        return Response(PerformanceSerializer(data, many=True).data)

    @extend_schema(
        parameters=[
            OpenApiParameter("service_position", int, required=True, description="Counter id."),
        ],
        responses=TillReaderSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def readers(self, request):
        """The till readers of the counter's seller, to choose from on the till page."""
        serializer = ReadersQuerySerializer(
            data=request.query_params, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        position = serializer.validated_data["service_position"]
        readers = services.seller_readers(position.seller_id)
        return Response(TillReaderSerializer(readers, many=True).data)

    @extend_schema(request=SetReaderSerializer, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"], url_path="reader")
    def set_reader(self, request, pk=None):
        """Use another of the seller's till readers for this draft."""
        purchase = self.get_object()
        serializer = SetReaderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.set_reader(purchase=purchase, reader=serializer.validated_data["reader"])
        return self._respond(purchase)

    @extend_schema(request=None, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"])
    def wait(self, request, pk=None):
        """ "Scan card to buy": the next tap on the purchase's reader (within 2 minutes) goes
        to this purchase; another purchase waiting on that reader stops waiting."""
        services.wait_for_card(purchase=self.get_object())
        return self._respond(self.get_object())

    @extend_schema(request=None, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"], url_path="stop-waiting")
    def stop_waiting(self, request, pk=None):
        """The seller closed the scan dialog: taps no longer go to this purchase."""
        services.stop_waiting(purchase=self.get_object())
        return self._respond(self.get_object())

    @extend_schema(request=ItemAddSerializer, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"], url_path="items")
    def add_item(self, request, pk=None):
        """Add a good (or increase its quantity). Courts are booked separately
        (`/bookings/checkout/`). Returns the whole purchase."""
        purchase = self.get_object()
        serializer = ItemAddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.add_item(purchase=purchase, **serializer.validated_data)
        return self._respond(purchase)

    @extend_schema(methods=["PATCH"], request=ItemUpdateSerializer, responses=PurchaseSerializer)
    @extend_schema(methods=["DELETE"], request=None, responses=PurchaseSerializer)
    @action(detail=True, methods=["patch", "delete"], url_path=r"items/(?P<item_id>\d+)")
    def item(self, request, pk=None, item_id=None):
        """PATCH changes the quantity; DELETE removes the item. Returns the whole purchase."""
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
