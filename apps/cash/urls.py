from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("cash/stores", views.CashStoreViewSet, basename="cash-store")
router.register("cash/entries", views.CashEntryViewSet, basename="cash-entry")

urlpatterns = router.urls
