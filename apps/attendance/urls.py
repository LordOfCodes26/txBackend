from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("attendance/records", views.AttendanceRecordViewSet, basename="attendance-record")
router.register("attendance/daily", views.DailyAttendanceViewSet, basename="attendance-daily")

urlpatterns = router.urls
