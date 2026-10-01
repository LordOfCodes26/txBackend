from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.developers.exceptions import DeveloperProfileNotFound
from apps.developers.models import Developer
from apps.goods.models import Good, GoodKind
from apps.sellers.access import CatalogPermission, SellerScopedQuerysetMixin
from apps.sellers.models import SellerStatus

from . import services
from .filters import BookingFilter
from .models import Booking
from .serializers import (
    AvailabilityQuerySerializer,
    BookingSerializer,
    RentalSerializer,
    SlotSerializer,
)


def _own_developer(request) -> Developer:
    developer = Developer.objects.filter(user=request.user).first()
    if developer is None:
        raise DeveloperProfileNotFound()
    return developer


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


class BookingViewSet(
    SellerScopedQuerysetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Court bookings (read-only). Bookings are made at the playground desk: add them to a
    draft purchase like any good
    (`POST /purchases/{id}/items/` with `start`); the developer taps their card and
    enters the PIN, and confirming the purchase creates the booking. Bookings are final.

    Sellers see bookings of their rentals; `purchase.view` sees all.
    """

    queryset = Booking.objects.select_related(
        "good__service_position__seller", "developer", "purchase__account_transaction"
    )
    serializer_class = BookingSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {"list": ["purchase.view"], "retrieve": ["purchase.view"]}
    seller_actions = ("list", "retrieve")
    scope_permission = "purchase.view"
    seller_lookup = "good__service_position__seller"
    filterset_class = BookingFilter
    ordering_fields = ["start", "created_at"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.good.service_position.seller_id
