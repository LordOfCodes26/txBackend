from django.urls import path

from .consumers import CounterConsumer, OccupancyConsumer

websocket_urlpatterns = [
    path("ws/counters/<int:position_id>/", CounterConsumer.as_asgi()),
    path("ws/occupancy/", OccupancyConsumer.as_asgi()),
]
