from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.rfid.scope import BuildingScopedMixin, sellers_with_positions_in
from common.permissions import HasPermissions

from . import onboarding, services
from .access import CatalogPermission, SellerScopedQuerysetMixin
from .exceptions import SellerProfileNotFound
from .filters import SellerFilter, ServicePositionFilter
from .models import Seller, ServicePosition
from .serializers import SellerSerializer, ServicePositionSerializer


def _count(rows, field: str):
    """How many of `rows` point to the outer row through `field` (0 when none)."""
    from django.db.models import Count, IntegerField, OuterRef, Subquery
    from django.db.models.functions import Coalesce

    counted = rows.filter(**{field: OuterRef("pk")}).order_by().values(field)
    return Coalesce(
        Subquery(counted.annotate(n=Count("pk")).values("n")[:1], output_field=IntegerField()), 0
    )


def _goods(**extra):
    from apps.goods.models import Good, GoodKind

    return Good.objects.filter(deleted_at__isnull=True, **extra).exclude(kind=GoodKind.RENTAL)


def _courts(**extra):
    from apps.goods.models import Good, GoodKind

    return Good.objects.filter(deleted_at__isnull=True, kind=GoodKind.RENTAL, **extra)


class SellerViewSet(
    BuildingScopedMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Sellers are never deleted; close them with `PATCH {"status": "CLOSED"}`."""

    queryset = Seller.objects.select_related("user")
    serializer_class = SellerSerializer

    def get_queryset(self):
        """Each store with its counts: counters, goods, courts and till readers."""
        from apps.rfid.models import DevicePurpose, RFIDDevice

        return (
            super()
            .get_queryset()
            .annotate(
                counters_count=_count(
                    ServicePosition.objects.filter(deleted_at__isnull=True, is_active=True),
                    "seller",
                ),
                goods_count=_count(_goods(), "service_position__seller"),
                courts_count=_count(_courts(), "service_position__seller"),
                till_readers_count=_count(
                    RFIDDevice.objects.filter(purpose=DevicePurpose.TILL, is_active=True), "seller"
                ),
            )
        )

    def filter_by_buildings(self, qs, scope):
        return qs.filter(pk__in=sellers_with_positions_in(scope).values("pk"))

    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["seller.view"],
        "retrieve": ["seller.view"],
        "create": ["seller.create"],
        "partial_update": ["seller.update"],
        "me": [],
        "onboard": ["seller.create"],
    }
    http_method_names = ["get", "post", "patch", "head", "options"]
    filterset_class = SellerFilter
    search_fields = ["name", "contact_name", "email"]
    ordering_fields = ["name", "status", "created_at"]

    def _check_cash(self, serializer):
        """Taking cash is switched on and off by whoever has `cash.enable` (admins)."""
        wanted = serializer.validated_data.get("accepts_cash")
        now = serializer.instance.accepts_cash if serializer.instance else False
        if (
            wanted is not None
            and wanted != now
            and not self.request.user.has_rbac_perm("cash.enable")
        ):
            raise PermissionDenied(_("Your account cannot switch cash sales on or off."))

    def perform_create(self, serializer):
        self._check_cash(serializer)
        serializer.instance = services.create_seller(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        self._check_cash(serializer)
        serializer.instance = services.update_seller(
            actor=self.request.user, seller=serializer.instance, **serializer.validated_data
        )

    @extend_schema(request=onboarding.StoreSerializer, responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["post"])
    def onboard(self, request):
        """New store in one step: its login (new seller user, existing one, or none), the
        seller, its first counter and optionally its till reader. All or nothing."""
        missing = [
            p
            for p in onboarding.required_permissions(request.data)
            if not request.user.has_rbac_perm(p)
        ]
        if missing:
            raise PermissionDenied()
        body = onboarding.StoreSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        return Response(
            onboarding.create_store(actor=request.user, data=body.validated_data), status=201
        )

    @extend_schema(responses=SellerSerializer)
    @action(detail=False, methods=["get"])
    def me(self, request):
        """The caller's own seller profile (as owner, or as a position manager)."""
        seller = (
            Seller.objects.filter(user=request.user).first()
            or Seller.objects.filter(positions__manager=request.user).first()
        )
        if seller is None:
            raise SellerProfileNotFound()
        return Response(SellerSerializer(seller).data)


class ServicePositionViewSet(BuildingScopedMixin, SellerScopedQuerysetMixin, viewsets.ModelViewSet):
    """Sellers manage their own positions; users with seller.* (e.g. ADMIN) manage all.
    DELETE is soft."""

    queryset = ServicePosition.objects.select_related("seller", "building", "manager").annotate(
        goods_count=_count(_goods(), "service_position"),
        courts_count=_count(_courts(), "service_position"),
    )
    serializer_class = ServicePositionSerializer
    permission_classes = [CatalogPermission]
    required_permissions = {
        "list": ["seller.view"],
        "retrieve": ["seller.view"],
        "create": ["counter.manage"],
        "partial_update": ["counter.manage"],
        "destroy": ["counter.manage"],
    }
    seller_actions = ("list", "retrieve", "create", "partial_update", "destroy")
    scope_permission = "seller.view"
    position_lookup = "pk"
    building_lookup = "building"
    owner_only_actions = ("create", "destroy")
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filterset_class = ServicePositionFilter
    search_fields = ["name", "location", "seller__name"]
    ordering_fields = ["name", "created_at"]

    @staticmethod
    def owner_seller_id(obj):
        return obj.seller_id

    @staticmethod
    def owner_position_id(obj):
        return obj.pk

    def perform_create(self, serializer):
        serializer.instance = services.create_position(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        serializer.instance = services.update_position(
            actor=self.request.user, position=serializer.instance, **serializer.validated_data
        )

    def perform_destroy(self, instance):
        services.delete_position(actor=self.request.user, position=instance)
