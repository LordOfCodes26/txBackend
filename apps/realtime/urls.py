from django.urls import path

from .views import TicketView

urlpatterns = [path("realtime/ticket/", TicketView.as_view(), name="realtime-ticket")]
