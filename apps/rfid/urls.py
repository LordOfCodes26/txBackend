from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("rfid/cards", views.RFIDCardViewSet, basename="rfid-card")
router.register("rfid/assignments", views.RFIDCardAssignmentViewSet, basename="rfid-assignment")
router.register("rfid/devices", views.RFIDDeviceViewSet, basename="rfid-device")
router.register("rfid/events", views.RFIDEventViewSet, basename="rfid-event")
router.register("rfid/buildings", views.BuildingViewSet, basename="rfid-building")

urlpatterns = [
    path("rfid/device/heartbeat/", views.DeviceHeartbeatView.as_view(), name="rfid-heartbeat"),
    path("rfid/card-reads/", views.CardReadView.as_view(), name="rfid-card-reads"),
    *router.urls,
]
