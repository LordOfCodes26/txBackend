from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.goods.models import Good, GoodKind
from apps.purchases import services as purchase_services
from apps.purchases.models import Purchase, PurchaseKind
from apps.purchases.serializers import ConfirmSerializer, PurchaseSerializer
from apps.rfid.scope import BuildingScopedMixin
from apps.sellers.access import CatalogPermission, SellerScopedQuerysetMixin
from apps.sellers.models import SellerStatus
from common.idempotency import HEADER, require_idempotency_key
from common.middleware import client_ip

from . import services
from .filters import BookingFilter
from .models import Booking
from .serializers import (
    AvailabilityQuerySerializer,
    BookingChangeSerializer,
    BookingSerializer,
    CheckoutCreateSerializer,
    RentalSerializer,
    ScheduleSerializer,
    SlotSerializer,
)


class RentalViewSet(viewsets.ReadOnlyModelViewSet):
    """Bookable rentals (playground, pool, ...), visible to every logged-in user."""

    queryset = (
        Good.objects.filter(
            Q(kind=GoodKind.RENTAL),
            Q(rental__isnull=False),
            Q(is_active=True),
            Q(service_position__is_active=True),
            Q(service_position__deleted_at__isnull=True),
            Q(service_position__seller__status=SellerStatus.ACTIVE),
        )
        .select_related("service_position__seller", "rental")
        .prefetch_related("images")
    )
    serializer_class = RentalSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = []
    search_fields = ["name", "description", "service_position__seller__name"]
    ordering_fields = ["name", "price"]

    @extend_schema(parameters=[AvailabilityQuerySerializer], responses=SlotSerializer(many=True))
    @action(detail=True, methods=["get"])
    def availability(self, request, pk=None):
        """All slots of one day and whether each can still be booked."""
        query = AvailabilityQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        slots = services.slots_for_day(self.get_object(), query.validated_data["date"])
        return Response(SlotSerializer(slots, many=True).data)

    @extend_schema(parameters=[AvailabilityQuerySerializer], responses=ScheduleSerializer)
    @action(detail=False, methods=["get"])
    def schedule(self, request):
        """One day for every court: booked and free periods, to find a blank time.
        Narrow it with `?search=` like the list."""
        query = AvailabilityQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data["date"]
        courts = [
            services.day_schedule(good, day)
            for good in self.filter_queryset(self.get_queryset()).order_by("name", "pk")
        ]
        return Response(ScheduleSerializer({"date": day, "courts": courts}).data)


class BookingViewSet(
    BuildingScopedMixin,
    SellerScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Court bookings. They are paid at the desk through `/bookings/checkout/` (card tap +
    PIN). Until it starts, a booking can be moved (`change`); there are no refunds.

    Sellers see bookings of their rentals; `purchase.view` sees all.
    """

    queryset = Booking.objects.select_related(
        "good__service_position__seller", "developer", "purchase__account_transaction"
    )
    serializer_class = BookingSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {
        "list": ["booking.view"],
        "retrieve": ["booking.view"],
        "change": ["booking.change"],
    }
    seller_actions = ("list", "retrieve", "change")
    lookup_value_regex = r"\d+"
    position_lookup = "good__service_position"
    building_lookup = "good__service_position__building"
    scope_permission = "booking.view"
    seller_lookup = "good__service_position__seller"
    filterset_class = BookingFilter
    ordering_fields = ["start", "created_at"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.good.service_position.seller_id

    @staticmethod
    def owner_position_id(obj):
        return obj.good.service_position_id

    @extend_schema(request=BookingChangeSerializer, responses=BookingSerializer)
    @action(detail=True, methods=["post"])
    def change(self, request, pk=None):
        """Move a paid booking to another date / time, and optionally another court of the
        same seller. Allowed until it starts; the new time must cost the same as paid."""
        booking = self.get_object()
        serializer = BookingChangeSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        booking = services.change_booking(
            actor=request.user,
            booking=booking,
            day=data["date"],
            start_time=data["start_time"],
            end_time=data["end_time"],
            good=data.get("good"),
        )
        return Response(BookingSerializer(self.queryset.get(pk=booking.pk)).data)


class BookingCheckoutViewSet(
    BuildingScopedMixin,
    SellerScopedQuerysetMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Pay for a court booking at the desk, separately from till sales.

    POST creates the checkout for one court and time; the developer taps their card on the
    desk's reader (`card_tapped` on the counter WebSocket, `presented_card` here) and enters
    the PIN; `confirm` books it for the card holder. The id is a purchase id.
    """

    queryset = (
        Purchase.objects.filter(kind=PurchaseKind.BOOKING)
        .select_related(
            "seller",
            "service_position",
            "developer",
            "card",
            "account_transaction",
            "presented_event__developer",
            "reader",
        )
        .prefetch_related("items__good__rental", "bookings")
    )
    serializer_class = PurchaseSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {
        "create": ["booking.create"],
        "retrieve": ["booking.view"],
        "confirm": ["booking.create"],
        "cancel": ["booking.cancel"],
        "wait": ["booking.create"],
        "stop_waiting": ["booking.create"],
    }
    seller_actions = ("create", "retrieve", "confirm", "cancel", "wait", "stop_waiting")
    scope_permission = "booking.view"
    position_lookup = "service_position"
    building_lookup = "service_position__building"

    @staticmethod
    def owner_seller_id(obj):
        return obj.seller_id

    @staticmethod
    def owner_position_id(obj):
        return obj.service_position_id

    def _respond(self, purchase, code=status.HTTP_200_OK):
        return Response(PurchaseSerializer(self.queryset.get(pk=purchase.pk)).data, status=code)

    @extend_schema(request=CheckoutCreateSerializer, responses={201: PurchaseSerializer})
    def create(self, request, *args, **kwargs):
        serializer = CheckoutCreateSerializer(
            data=request.data, context=self.get_serializer_context()
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        purchase = services.start_checkout(
            actor=request.user,
            good=data["good"],
            day=data["date"],
            start_time=data["start_time"],
            end_time=data["end_time"],
            reader=data.get("reader"),
            client_ip=client_ip(request),
        )
        return self._respond(purchase, status.HTTP_201_CREATED)

    @extend_schema(
        request=ConfirmSerializer,
        responses={201: PurchaseSerializer, 200: PurchaseSerializer},
        parameters=[
            OpenApiParameter(
                HEADER,
                location=OpenApiParameter.HEADER,
                required=True,
                description="Unique per payment attempt; a retry with the same key is safe.",
            )
        ],
        description="Charge the developer who tapped and book the court for them. 201 when "
        "booked now; 200 when this Idempotency-Key already did.",
    )
    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        key = require_idempotency_key(request)
        purchase = self.get_object()
        serializer = ConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        purchase, created = purchase_services.confirm_purchase(
            actor=request.user, purchase=purchase, idempotency_key=key, **serializer.validated_data
        )
        return self._respond(purchase, status.HTTP_201_CREATED if created else status.HTTP_200_OK)

    @extend_schema(request=None, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Abandon the checkout before paying."""
        purchase = purchase_services.cancel_purchase(actor=request.user, purchase=self.get_object())
        return self._respond(purchase)

    @extend_schema(request=None, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"])
    def wait(self, request, pk=None):
        """ "Scan card to book": the next tap on the desk's reader (within 2 minutes) goes to
        this booking."""
        return self._respond(purchase_services.wait_for_card(purchase=self.get_object()))

    @extend_schema(request=None, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"], url_path="stop-waiting")
    def stop_waiting(self, request, pk=None):
        """The desk closed the scan dialog: taps no longer go to this booking."""
        return self._respond(purchase_services.stop_waiting(purchase=self.get_object()))
