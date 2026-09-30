from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from common.permissions import HasPermissions

from . import services
from .exceptions import DeveloperProfileNotFound
from .filters import DeveloperFilter
from .models import Developer
from .serializers import DeveloperSerializer, MyDeveloperProfileSerializer


class DeveloperViewSet(viewsets.ModelViewSet):
    """DELETE is a soft delete. For someone leaving the company, set `status=TERMINATED`."""

    queryset = Developer.objects.select_related("manager")
    serializer_class = DeveloperSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["developer.view"],
        "retrieve": ["developer.view"],
        "create": ["developer.create"],
        "partial_update": ["developer.update"],
        "destroy": ["developer.delete"],
        "me": [],
    }
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    filterset_class = DeveloperFilter
    search_fields = ["full_name", "email", "employee_number", "department", "position_title"]
    ordering_fields = ["full_name", "employee_number", "department", "start_date", "created_at"]

    def perform_create(self, serializer):
        serializer.instance = services.create_developer(
            actor=self.request.user, **serializer.validated_data
        )

    def perform_update(self, serializer):
        serializer.instance = services.update_developer(
            actor=self.request.user, developer=serializer.instance, **serializer.validated_data
        )

    def perform_destroy(self, instance):
        services.delete_developer(actor=self.request.user, developer=instance)

    @extend_schema(responses=MyDeveloperProfileSerializer)
    @action(detail=False, methods=["get"])
    def me(self, request):
        developer = Developer.objects.select_related("manager").filter(user=request.user).first()
        if developer is None:
            raise DeveloperProfileNotFound()
        return Response(MyDeveloperProfileSerializer(developer).data, status=status.HTTP_200_OK)
