from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("sellers", views.SellerViewSet, basename="seller")
router.register("service-positions", views.ServicePositionViewSet, basename="service-position")

urlpatterns = router.urls
