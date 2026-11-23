from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("cash/stores", views.CashStoreViewSet, basename="cash-store")

urlpatterns = router.urls
