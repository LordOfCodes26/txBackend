from drf_spectacular.utils import extend_schema
from rest_framework import generics, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.developers.exceptions import DeveloperProfileNotFound
from apps.developers.models import Developer
from apps.rfid.scope import BuildingScopedMixin, building_scope, ensure_in_scope
from common.permissions import HasPermissions

from . import occupancy, services
from .filters import AttendanceRecordFilter, DailyAttendanceFilter
from .models import AttendanceRecord, DailyAttendance
from .serializers import (
    AttendanceRecordSerializer,
    DailyAttendanceSerializer,
    ManualRecordSerializer,
    OccupancySerializer,
    PersonInsideSerializer,
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
    BuildingScopedMixin,
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
    building_lookup = "developer__building"
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
        ensure_in_scope(
            request.user,
            "attendance.correct",
            serializer.validated_data["developer"].building_id,
            field="developer",
        )
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


class DailyAttendanceViewSet(BuildingScopedMixin, OwnDataMixin, viewsets.ReadOnlyModelViewSet):
    queryset = DailyAttendance.objects.select_related("developer")
    serializer_class = DailyAttendanceSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"list": ["attendance.view"], "retrieve": ["attendance.view"], "me": []}
    building_lookup = "developer__building"
    filterset_class = DailyAttendanceFilter
    search_fields = ["developer__full_name", "developer__employee_number"]
    ordering_fields = ["work_date", "first_seen", "worked_seconds"]


class OccupancyView(APIView):
    """How many developers are inside each building right now, and in total."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": ["attendance.view"]}

    @extend_schema(responses=OccupancySerializer)
    def get(self, request):
        return Response(
            occupancy.occupancy(buildings=building_scope(request.user, "attendance.view"))
        )


class OccupancyPeopleView(generics.ListAPIView):
    """Who is inside right now. `?building=<id>` (or `none`), `?department=`, `?search=`."""

    serializer_class = PersonInsideSerializer
    permission_classes = [HasPermissions]
    required_permissions = {"get": ["attendance.view"]}
    filter_backends = []

    def get_queryset(self):
        qs = occupancy.inside().order_by("developer__full_name", "developer_id")
        scope = building_scope(self.request.user, "attendance.view")
        if scope is not None:
            qs = qs.filter(building_id__in=scope)
        params = self.request.query_params
        building = params.get("building")
        if building == "none":
            qs = qs.filter(building__isnull=True)
        elif building:
            qs = qs.filter(building_id=building)
        if params.get("department"):
            qs = qs.filter(developer__department__iexact=params["department"])
        if params.get("search"):
            term = params["search"]
            qs = qs.filter(developer__full_name__icontains=term) | qs.filter(
                developer__employee_number__icontains=term
            )
        return qs
