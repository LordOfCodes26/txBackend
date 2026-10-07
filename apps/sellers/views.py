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


class SellerViewSet(
    BuildingScopedMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Sellers are never deleted; close them with `PATCH {"status": "CLOSED"}`."""

    queryset = Seller.objects.all()
    serializer_class = SellerSerializer

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

    def perform_create(self, serializer):
        serializer.instance = services.create_seller(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        serializer.instance = services.update_seller(
            actor=self.request.user, seller=serializer.instance, **serializer.validated_data
        )

    @extend_schema(request=onboarding.StoreSerializer, responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=["post"])
    def onboard(self, request):
        """New store in one step: its login (new SELLER user, existing one, or none), the
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

    queryset = ServicePosition.objects.select_related("seller")
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
