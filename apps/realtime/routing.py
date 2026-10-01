from django.urls import path

from .consumers import CounterConsumer

websocket_urlpatterns = [
    path("ws/counters/<int:position_id>/", CounterConsumer.as_asgi()),
]
