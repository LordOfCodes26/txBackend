from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("goods", views.GoodViewSet, basename="good")
router.register(
    "inventory/movements", views.InventoryMovementViewSet, basename="inventory-movement"
)

urlpatterns = router.urls
