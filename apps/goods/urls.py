from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("goods", views.GoodViewSet, basename="good")
router.register("good-categories", views.GoodCategoryViewSet, basename="good-category")
router.register(
    "inventory/movements", views.InventoryMovementViewSet, basename="inventory-movement"
)

urlpatterns = router.urls
