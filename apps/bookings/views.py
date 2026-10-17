from django.db.models import Q
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.goods.models import Good, GoodKind
from apps.purchases import services as purchase_services
from apps.purchases.models import Purchase, PurchaseKind
from apps.purchases.serializers import (
    ConfirmSerializer,
    PurchaseSerializer,
    SetReaderSerializer,
    TillReaderSerializer,
)
from apps.rfid.models import DevicePurpose, RFIDDevice
from apps.rfid.scope import BuildingScopedMixin
from apps.sellers.access import (
    PLAYGROUND,
    CatalogPermission,
    SellerScopedQuerysetMixin,
    acting_positions,
    acting_seller,
    works_playground,
)
from apps.sellers.models import Seller, SellerStatus
from common.idempotency import HEADER, require_idempotency_key
from common.middleware import client_ip
from common.permissions import HasPermissions

from . import services
from .filters import BookingFilter
from .models import Booking, BookingStatus, PlaygroundCheck
from .serializers import (
    AvailabilityQuerySerializer,
    BookingChangeSerializer,
    BookingSerializer,
    CheckoutCreateSerializer,
    DeskReaderSerializer,
    DeskReadersQuerySerializer,
    PayBookingSerializer,
    PlaygroundCheckSerializer,
    PlaygroundChecksQuerySerializer,
    RentalSerializer,
    ReserveSerializer,
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
        .prefetch_related("images", "court_prices")
    )
    serializer_class = RentalSerializer
    permission_classes = [IsAuthenticated]
    filterset_fields = []
    search_fields = ["name", "description", "service_position__seller__name"]
    ordering_fields = ["name", "price"]

    def get_queryset(self):
        """Everyone sees every court (playground sellers too: the playground is shared),
        except a store's goods-only login: its own courts, if any."""
        qs = super().get_queryset()
        user = self.request.user
        if user.has_rbac_perm("booking.view") or works_playground(user):
            return qs
        seller = acting_seller(user)
        if seller is None:
            return qs
        qs = qs.filter(service_position__seller=seller)
        positions = acting_positions(user)
        return qs if positions is None else qs.filter(service_position__in=positions)

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
        """One day for every court: booked and free periods, to find a blank time; for the
        desk also the bookings (who, paid or not). Narrow it with `?search=` like the list."""
        query = AvailabilityQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        day = query.validated_data["date"]
        # Who booked is for the desk only (booking.view or a playground seller).
        desk = request.user.has_rbac_perm("booking.view") or works_playground(request.user)
        courts = [
            services.day_schedule(good, day, with_bookings=desk)
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
    PIN). Until it starts, a booking can be moved (`change`), or deleted with its purchase
    (`DELETE /purchases/{id}/`, refunded).

    Sellers see bookings of their rentals; `purchase.view` sees all.
    """

    queryset = Booking.objects.select_related(
        "good__service_position__seller",
        "developer",
        "purchase__account_transaction",
        "checked_in_at_store",
        "court_price",
    )
    serializer_class = BookingSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {
        "list": ["booking.view"],
        "retrieve": ["booking.view"],
        "change": ["booking.change"],
        "reserve": ["booking.create"],
        "pay": ["booking.create"],
        "cancel": ["booking.cancel"],
        "check_in": ["booking.create"],
        "now": ["booking.view"],
        "arrival": ["booking.view"],
        "people": ["booking.create"],
    }
    seller_actions = (
        "list",
        "retrieve",
        "change",
        "reserve",
        "pay",
        "cancel",
        "check_in",
        "now",
        "arrival",
        "people",
    )
    seller_area = PLAYGROUND
    playground_filter = Q()  # every booking: the playground is shared
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

    def _booking(self, booking):
        return Response(BookingSerializer(self.queryset.get(pk=booking.pk)).data)

    @extend_schema(request=ReserveSerializer, responses={201: BookingSerializer})
    @action(detail=False, methods=["post"])
    def reserve(self, request):
        """Book a court for free: the time is held; the developer pays on arrival
        (`pay`). No money moves now."""
        serializer = ReserveSerializer(data=request.data, context=self.get_serializer_context())
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        booking = services.reserve(
            actor=request.user,
            good=data["good"],
            developer=data["developer"],
            day=data["date"],
            start_time=data["start_time"],
            end_time=data["end_time"],
            people=data["people"],
            price=data.get("price"),
        )
        return Response(BookingSerializer(self.queryset.get(pk=booking.pk)).data, status=201)

    @extend_schema(request=PayBookingSerializer, responses={201: PurchaseSerializer})
    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        """Pay a reserved booking on arrival: a checkout (a purchase) for its court and time;
        the arrival tap (`event`) counts as the card, so the developer only types the PIN
        (`/bookings/checkout/{id}/confirm/`). Paying checks them in."""
        booking = self.get_object()
        serializer = PayBookingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        purchase = services.start_checkout(
            actor=request.user,
            booking=booking,
            reader=serializer.validated_data.get("reader"),
            event=serializer.validated_data.get("event"),
            client_ip=client_ip(request),
        )
        return Response(
            PurchaseSerializer(BookingCheckoutViewSet.queryset.get(pk=purchase.pk)).data, status=201
        )

    @extend_schema(request=None, responses={204: None})
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancel a booking: an unpaid one is dropped; a paid one is refunded (before it
        starts, or later if the court allows it; never after check-in)."""
        booking = self.get_object()
        if booking.status == BookingStatus.RESERVED:
            services.cancel_reservation(actor=request.user, booking=booking)
        else:
            purchase_services.refund_purchase(actor=request.user, purchase=booking.purchase)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=None, responses=BookingSerializer)
    @action(detail=True, methods=["post"], url_path="check-in")
    def check_in(self, request, pk=None):
        """The developer is at the desk: a paid booking is checked in."""
        return self._booking(services.desk_check_in(actor=request.user, booking=self.get_object()))

    @extend_schema(responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["get"])
    def now(self, request):
        """Every court now: FREE (until when), IN_USE (by whom, until when, paid or not) or
        CLOSED, and its next booking today."""
        return Response(
            {"courts": services.now_board(RentalViewSet.queryset.order_by("name", "pk"))}
        )

    @extend_schema(
        parameters=[
            OpenApiParameter("search", str, required=False),
            OpenApiParameter("id", int, required=False),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    @action(detail=False, methods=["get"])
    def people(self, request):
        """Who a desk booking is for: `?search=` (name or employee number) or `?id=`."""
        raw = request.query_params.get("id")
        pk = int(raw) if raw and raw.isdigit() else None
        return Response(
            {"results": services.desk_people(search=request.query_params.get("search", ""), pk=pk)}
        )

    @extend_schema(
        parameters=[
            OpenApiParameter("reader", str, description="The desk reader's code."),
            OpenApiParameter("after", int, description="Only taps newer than this event id."),
        ],
        responses=OpenApiTypes.OBJECT,
    )
    @action(detail=False, methods=["get"])
    def arrival(self, request):
        """The desk's arrival tap: `cursor` (newest tap id); with `after`, the newest tap
        after it: who, their booking now and later today."""
        reader = RFIDDevice.objects.filter(
            code=request.query_params.get("reader", ""),
            purpose__in=[DevicePurpose.TILL, DevicePurpose.ENROLL],
            is_active=True,
        ).first()
        if reader is None:
            return Response({"cursor": None, "tap": None})
        try:
            after = int(request.query_params["after"]) if "after" in request.query_params else None
        except ValueError:
            after = None
        return Response(services.arrival(reader, after))

    @extend_schema(request=BookingChangeSerializer, responses=BookingSerializer)
    @action(detail=True, methods=["post"])
    def change(self, request, pk=None):
        """Move a paid booking to another date / time, and optionally another court of the
        same seller. Allowed until it starts (or later if the court allows
        `refund_after_start`). A cheaper time refunds the difference; a dearer one charges it
        and needs the developer's `pin`."""
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
            pin=data.get("pin") or None,
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
        "readers": ["booking.create"],
        "set_reader": ["booking.create"],
    }
    seller_actions = (
        "create",
        "retrieve",
        "confirm",
        "cancel",
        "wait",
        "stop_waiting",
        "readers",
        "set_reader",
    )
    seller_area = PLAYGROUND
    playground_filter = Q()  # every booking payment: the playground is shared
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
            good=data.get("good"),
            day=data.get("date"),
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
            reader=data.get("reader"),
            client_ip=client_ip(request),
            people=data["people"],
            now_slots=data.get("now_slots"),
            booking=data.get("booking"),
            event=data.get("event"),
            price=data.get("price"),
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

    @extend_schema(
        parameters=[DeskReadersQuerySerializer], responses=DeskReaderSerializer(many=True)
    )
    @action(detail=False, methods=["get"])
    def readers(self, request):
        """The readers a court's booking can take the card tap from, to choose at the desk:
        the till readers of the court's store, then every card assign (master) reader."""
        query = DeskReadersQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        seller_id = query.validated_data["good"].service_position.seller_id
        readers = purchase_services.usable_readers(seller_id, PurchaseKind.BOOKING)
        return Response(DeskReaderSerializer(readers, many=True).data)

    @extend_schema(request=SetReaderSerializer, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"], url_path="reader")
    def set_reader(self, request, pk=None):
        """Take the card tap from another reader (a till or card assign reader)."""
        purchase = self.get_object()
        serializer = SetReaderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        purchase_services.set_reader(purchase=purchase, reader=serializer.validated_data["reader"])
        return self._respond(purchase)

    @extend_schema(request=None, responses=PurchaseSerializer)
    @action(detail=True, methods=["post"], url_path="stop-waiting")
    def stop_waiting(self, request, pk=None):
        """The desk closed the scan dialog: taps no longer go to this booking."""
        return self._respond(purchase_services.stop_waiting(purchase=self.get_object()))


def checking_store(user):
    """The active store that checks bookings whose login (or counter manager) the user is."""
    return (
        Seller.objects.filter(checks_bookings=True, status=SellerStatus.ACTIVE)
        .filter(Q(user=user) | Q(positions__manager=user))
        .distinct()
        .first()
    )


class PlaygroundCheckViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Booking checks, newest first. A store that checks bookings (e.g. the gym counter):
    a tap on its till reader, with no sale waiting for it, checks the developer in to the
    booking running now (or starting within PLAYGROUND_CHECK_IN_EARLY_MINUTES). Its
    checkers (the store's login or counter manager, with `booking.check`) see its taps;
    users with `reader.view` see every store's. Poll with `?after=<last id>`."""

    queryset = PlaygroundCheck.objects.select_related(
        "event__developer", "developer", "booking__good", "reader", "seller"
    )
    serializer_class = PlaygroundCheckSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"list": ["booking.check"], "readers": ["booking.check"]}

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not user.has_rbac_perm("reader.view"):
            store = checking_store(user)
            qs = qs.filter(seller=store) if store else qs.none()
        query = PlaygroundChecksQuerySerializer(data=self.request.query_params)
        query.is_valid(raise_exception=True)
        after = query.validated_data.get("after")
        return qs if after is None else qs.filter(pk__gt=after)

    @extend_schema(parameters=[PlaygroundChecksQuerySerializer])
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(responses=TillReaderSerializer(many=True))
    @action(detail=False, methods=["get"])
    def readers(self, request):
        """The till readers whose taps check bookings for this user: their store's."""
        readers = RFIDDevice.objects.filter(
            purpose=DevicePurpose.TILL, is_active=True, seller__checks_bookings=True
        )
        if not request.user.has_rbac_perm("reader.view"):
            store = checking_store(request.user)
            readers = readers.filter(seller=store) if store else readers.none()
        return Response(TillReaderSerializer(readers.order_by("code"), many=True).data)
