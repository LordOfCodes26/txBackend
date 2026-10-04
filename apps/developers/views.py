from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.rfid.scope import BuildingScopedMixin, ensure_in_scope
from common.permissions import HasPermissions

from . import services
from .exceptions import DeveloperProfileNotFound
from .filters import DeveloperFilter
from .models import Developer
from .serializers import DeveloperSerializer, MyDeveloperProfileSerializer


class DeveloperViewSet(BuildingScopedMixin, viewsets.ModelViewSet):
    """DELETE is a soft delete. For someone leaving the company, set `status=TERMINATED`.

    Building managers see and manage only developers of their buildings.
    """

    queryset = Developer.objects.select_related("building")
    serializer_class = DeveloperSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["developer.view"],
        "retrieve": ["developer.view"],
        "create": ["developer.create"],
        "partial_update": ["developer.update"],
        "destroy": ["developer.delete"],
        "me": [],
        "departments": ["developer.view"],
    }
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filterset_class = DeveloperFilter
    search_fields = ["full_name", "employee_number", "department", "position_title", "phone"]
    ordering_fields = [
        "full_name",
        "employee_number",
        "department",
        "start_date",
        "out_date",
        "birthday",
        "created_at",
    ]

    def perform_create(self, serializer):
        ensure_in_scope(
            self.request.user,
            "developer.create",
            serializer.validated_data.get("building") and serializer.validated_data["building"].pk,
        )
        serializer.instance = services.create_developer(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        if "building" in serializer.validated_data:
            building = serializer.validated_data["building"]
            ensure_in_scope(self.request.user, "developer.update", building and building.pk)
        serializer.instance = services.update_developer(
            actor=self.request.user, developer=serializer.instance, **serializer.validated_data
        )

    def perform_destroy(self, instance):
        services.delete_developer(actor=self.request.user, developer=instance)

    @extend_schema(responses={200: {"type": "array", "items": {"type": "string"}}})
    @action(detail=False, methods=["get"])
    def departments(self, request):
        """The departments already used (sorted, no blanks), to pick from when typing one."""
        names = (
            self.get_queryset()
            .exclude(department="")
            .order_by("department")
            .values_list("department", flat=True)
            .distinct()
        )
        return Response(list(names))

    @extend_schema(responses=MyDeveloperProfileSerializer)
    @action(detail=False, methods=["get"])
    def me(self, request):
        developer = Developer.objects.filter(user=request.user).first()
        if developer is None:
            raise DeveloperProfileNotFound()
        return Response(MyDeveloperProfileSerializer(developer).data, status=status.HTTP_200_OK)
