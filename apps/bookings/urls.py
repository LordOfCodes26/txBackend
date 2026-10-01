from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("rentals", views.RentalViewSet, basename="rental")
router.register("bookings/checkout", views.BookingCheckoutViewSet, basename="booking-checkout")
router.register("bookings", views.BookingViewSet, basename="booking")

urlpatterns = router.urls
