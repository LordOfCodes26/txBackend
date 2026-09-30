from django.urls import path
from rest_framework.routers import SimpleRouter

from . import views

router = SimpleRouter()
router.register("finance/accounts", views.DeveloperAccountViewSet, basename="finance-account")
router.register(
    "finance/transactions", views.AccountTransactionViewSet, basename="finance-transaction"
)

urlpatterns = [
    path("finance/deposits/", views.DepositView.as_view(), name="finance-deposit"),
    path("finance/adjustments/", views.AdjustmentView.as_view(), name="finance-adjustment"),
    *router.urls,
]
