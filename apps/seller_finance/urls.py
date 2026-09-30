from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("seller-finance/accounts", views.SellerAccountViewSet, basename="seller-account")
router.register(
    "seller-finance/transactions", views.SellerTransactionViewSet, basename="seller-transaction"
)
router.register("seller-finance/payouts", views.SellerPaymentViewSet, basename="seller-payout")

urlpatterns = [
    path(
        "seller-finance/adjustments/",
        views.SellerAdjustmentView.as_view(),
        name="seller-adjustment",
    ),
    *router.urls,
]
