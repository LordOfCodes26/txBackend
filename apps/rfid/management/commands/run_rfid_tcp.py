import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand

from apps.rfid.tcp import DoorTCPServer


class Command(BaseCommand):
    help = "Run the raw TCP listener for door devices ($-framed JSON, see apps/rfid/tcp.py)."

    def add_arguments(self, parser):
        parser.add_argument("--host", default=settings.RFID_TCP_HOST)
        parser.add_argument("--port", type=int, default=settings.RFID_TCP_PORT)

    def handle(self, *args, host, port, **options):
        self.stdout.write(f"RFID TCP listener on {host}:{port}")
        asyncio.run(DoorTCPServer(host, port).serve_forever())
