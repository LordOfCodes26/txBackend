from django.urls import path

from . import views

urlpatterns = [
    path("stats/", views.CompanyStatsView.as_view(), name="company-stats"),
    path("stats/finance/", views.FinanceStatsView.as_view(), name="finance-stats"),
    path(
        "stats/finance/sellers/<int:pk>/",
        views.SellerFinanceStatsView.as_view(),
        name="seller-finance-stats",
    ),
]
