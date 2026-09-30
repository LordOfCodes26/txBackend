from rest_framework.routers import SimpleRouter

from .views import DeveloperViewSet

router = SimpleRouter()
router.register("developers", DeveloperViewSet, basename="developer")

urlpatterns = router.urls
