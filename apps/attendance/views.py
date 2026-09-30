from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.developers.exceptions import DeveloperProfileNotFound
from apps.developers.models import Developer
from common.permissions import HasPermissions

from . import services
from .filters import AttendanceRecordFilter, DailyAttendanceFilter
from .models import AttendanceRecord, DailyAttendance
from .serializers import (
    AttendanceRecordSerializer,
    DailyAttendanceSerializer,
    ManualRecordSerializer,
    VoidSerializer,
)


class OwnDataMixin:
    """`me` lists the caller's own rows (any authenticated user with a developer profile)."""

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "me":
            developer = Developer.objects.filter(user=self.request.user).first()
            if developer is None:
                raise DeveloperProfileNotFound()
            qs = qs.filter(developer=developer)
        return qs

    @action(detail=False, methods=["get"])
    def me(self, request):
        return self.list(request)


class AttendanceRecordViewSet(
    OwnDataMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Records are never deleted; wrong ones are voided with a reason."""

    queryset = AttendanceRecord.objects.select_related("developer", "device")
    serializer_class = AttendanceRecordSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["attendance.view"],
        "retrieve": ["attendance.view"],
        "create": ["attendance.correct"],
        "void": ["attendance.correct"],
        "me": [],
    }
    filterset_class = AttendanceRecordFilter
    search_fields = ["developer__full_name", "developer__employee_number"]
    ordering_fields = ["event_time", "work_date"]

    @extend_schema(
        request=ManualRecordSerializer,
        responses={201: AttendanceRecordSerializer},
        description="Add a missing attendance moment (e.g. the developer forgot to scan).",
    )
    def create(self, request, *args, **kwargs):
        serializer = ManualRecordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record = services.add_manual_record(actor=request.user, **serializer.validated_data)
        return Response(AttendanceRecordSerializer(record).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=VoidSerializer, responses=AttendanceRecordSerializer)
    @action(detail=True, methods=["post"])
    def void(self, request, pk=None):
        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        record = services.void_record(
            actor=request.user, record=self.get_object(), **serializer.validated_data
        )
        return Response(AttendanceRecordSerializer(record).data)


class DailyAttendanceViewSet(OwnDataMixin, viewsets.ReadOnlyModelViewSet):
    queryset = DailyAttendance.objects.select_related("developer")
    serializer_class = DailyAttendanceSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"list": ["attendance.view"], "retrieve": ["attendance.view"], "me": []}
    filterset_class = DailyAttendanceFilter
    search_fields = ["developer__full_name", "developer__employee_number"]
    ordering_fields = ["work_date", "first_seen", "worked_seconds"]
