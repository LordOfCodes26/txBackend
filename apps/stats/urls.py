from django.urls import path

from . import views

urlpatterns = [path("stats/", views.CompanyStatsView.as_view(), name="company-stats")]
