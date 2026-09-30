from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("purchases", views.PurchaseViewSet, basename="purchase")

urlpatterns = router.urls
