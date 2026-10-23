from django.db.models import Count, F, OuterRef, Q
from django.db.models.functions import TruncWeek
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
from .models import AttendanceRecord, DailyAttendance, EventType, RecordSource
from .serializers import (
    AttendanceRecordSerializer,
    AttendanceRecordSummarySerializer,
    AttendanceStatisticsQuerySerializer,
    AttendanceStatisticsSerializer,
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

    queryset = AttendanceRecord.objects.select_related("developer__building", "device__building")
    serializer_class = AttendanceRecordSerializer
    permission_classes = [HasPermissions]
    required_permissions = {
        "list": ["attendance.view"],
        "retrieve": ["attendance.view"],
        "summary": ["attendance.view"],
        "create": ["attendance.correct"],
        "void": ["attendance.correct"],
        "me": [],
    }
    building_lookup = "developer__building"
    filterset_class = AttendanceRecordFilter
    # `card_label`: the card the developer holds now (annotated below).
    search_fields = [
        "developer__full_name",
        "developer__employee_number",
        "card_label",
        "device__code",
        "device__name",
    ]
    ordering_fields = [
        "event_time",
        "work_date",
        "event_type",
        "source",
        "is_void",
        "developer__employee_number",
        "developer__full_name",
        "developer__department",
        "developer__building__name",
        "device__code",
        "device__building__name",
        "card_label",
    ]

    def get_queryset(self):
        from apps.developers.views import with_card_label

        qs = super().get_queryset()
        return qs.annotate(
            card_label=with_card_label(Developer.objects.all())
            .filter(pk=OuterRef("developer"))
            .values("card_label")[:1]
        )

    @extend_schema(responses=AttendanceRecordSummarySerializer)
    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Counts for the records the list's filters and search select: records, in, out,
        manual, void and different developers."""
        qs = self.filter_queryset(self.get_queryset()).order_by()
        counts = qs.aggregate(
            records=Count("pk"),
            ins=Count("pk", filter=Q(event_type=EventType.IN, is_void=False)),
            outs=Count("pk", filter=Q(event_type=EventType.OUT, is_void=False)),
            manual=Count("pk", filter=Q(source=RecordSource.MANUAL)),
            void=Count("pk", filter=Q(is_void=True)),
            developers=Count("developer", distinct=True),
        )
        return Response(AttendanceRecordSummarySerializer(counts).data)

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
        from apps.developers.views import with_card_label

        qs = occupancy.inside().order_by("developer__full_name", "developer_id")
        # The card each person holds now (its printed number) and their home building.
        qs = qs.select_related("developer__building").annotate(
            card_label=with_card_label(Developer.objects.all())
            .filter(pk=OuterRef("developer"))
            .values("card_label")[:1]
        )
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


class AttendanceStatisticsView(APIView):
    """The attendance statistics page's figures for `date_from`..`date_to`, counted by the
    database however many scans the range has. Narrowed like the lists (a building
    manager: the developers of their buildings). `building` keeps the scans at that
    building's doors (or `none`: at no building) and the developers who made them."""

    permission_classes = [HasPermissions]
    required_permissions = {"get": ["attendance.view"]}

    @extend_schema(
        parameters=[AttendanceStatisticsQuerySerializer],
        responses=AttendanceStatisticsSerializer,
    )
    def get(self, request):
        query = AttendanceStatisticsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        first, last = query.validated_data["date_from"], query.validated_data["date_to"]
        building = query.validated_data.get("building")

        scans = AttendanceRecord.objects.filter(
            work_date__range=(first, last), is_void=False
        ).order_by()
        days = DailyAttendance.objects.filter(work_date__range=(first, last)).order_by()
        scope = building_scope(request.user, "attendance.view")
        if scope is not None:
            scans = scans.filter(developer__building__in=scope)
            days = days.filter(developer__building__in=scope)

        by_building = [
            {
                "building": row["device__building"],
                "name": row["device__building__name"],
                "people": row["people"],
                "ins": row["ins"],
                "outs": row["outs"],
            }
            for row in scans.values("device__building", "device__building__name")
            .annotate(
                people=Count("developer", distinct=True),
                ins=Count("pk", filter=Q(event_type=EventType.IN)),
                outs=Count("pk", filter=Q(event_type=EventType.OUT)),
            )
            .order_by("device__building__name", "device__building")
        ]
        if building == "none":
            scans = scans.filter(device__building__isnull=True)
        elif building:
            scans = scans.filter(device__building=int(building))
        if building:
            days = days.filter(developer__in=scans.values("developer"))

        weekly = (last - first).days + 1 > 31
        periods = (
            days.annotate(period=TruncWeek("work_date"))
            if weekly
            else days.annotate(period=F("work_date"))
        )
        counts = scans.aggregate(
            ins=Count("pk", filter=Q(event_type=EventType.IN)),
            outs=Count("pk", filter=Q(event_type=EventType.OUT)),
        )
        data = {
            "developers": days.aggregate(n=Count("developer", distinct=True))["n"],
            **counts,
            "weekly": weekly,
            "by_period": [
                {"name": row["period"].isoformat(), "people": row["people"]}
                for row in periods.values("period")
                .annotate(people=Count("developer", distinct=True))
                .order_by("period")
            ],
            "by_building": by_building,
            "by_department": [
                {"name": row["developer__department"], "people": row["people"]}
                for row in days.values("developer__department")
                .annotate(people=Count("developer", distinct=True))
                .order_by("-people", "developer__department")
            ],
        }
        return Response(AttendanceStatisticsSerializer(data).data)
