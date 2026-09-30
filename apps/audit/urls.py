from rest_framework.routers import SimpleRouter

from .views import AuditLogViewSet

router = SimpleRouter()
router.register("audit-logs", AuditLogViewSet, basename="audit-log")

urlpatterns = router.urls
