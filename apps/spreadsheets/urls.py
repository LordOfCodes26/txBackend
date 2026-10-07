from django.urls import path

from . import views

urlpatterns = [
    path("imports/<str:kind>/", views.ImportView.as_view(), name="spreadsheet-import"),
    path("imports/<str:kind>/template/", views.TemplateView.as_view(), name="spreadsheet-template"),
    path("exports/<str:kind>/", views.ExportView.as_view(), name="spreadsheet-export"),
]
