from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("attendance/records", views.AttendanceRecordViewSet, basename="attendance-record")
router.register("attendance/daily", views.DailyAttendanceViewSet, basename="attendance-daily")

urlpatterns = [
    path("attendance/occupancy/", views.OccupancyView.as_view(), name="attendance-occupancy"),
    path(
        "attendance/occupancy/people/",
        views.OccupancyPeopleView.as_view(),
        name="attendance-occupancy-people",
    ),
    *router.urls,
]
