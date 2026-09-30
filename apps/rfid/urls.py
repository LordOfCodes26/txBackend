from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("rfid/cards", views.RFIDCardViewSet, basename="rfid-card")
router.register("rfid/assignments", views.RFIDCardAssignmentViewSet, basename="rfid-assignment")
router.register("rfid/devices", views.RFIDDeviceViewSet, basename="rfid-device")
router.register("rfid/events", views.RFIDEventViewSet, basename="rfid-event")

urlpatterns = router.urls
