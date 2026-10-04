"""The demo device layout, shared by the seed_* commands (DEBUG only).

Matches the real installation: Building 1 has door units Door1-1..4 and Building 2 has
Door2-1..6, all sending the door's ID (Door1 / Door2) from their own fixed IP; each unit
is either a way in or a way out. Till readers Reader1-4 belong to the demo sellers; the card
assign readers are Master1 and Master2.
"""

from apps.accounts.models import User
from apps.rfid import services as rfid
from apps.rfid.models import Building, DevicePurpose, RFIDDevice
from apps.sellers.models import Seller

BUILDINGS = [("B1", "Building 1"), ("B2", "Building 2")]
# (building code, door ID, unit name, IP, direction)
DOOR_UNITS = [
    ("B1", "Door1", "Door1-1", "192.168.100.151", "IN"),
    ("B1", "Door1", "Door1-2", "192.168.100.152", "IN"),
    ("B1", "Door1", "Door1-3", "192.168.100.153", "OUT"),
    ("B1", "Door1", "Door1-4", "192.168.100.154", "OUT"),
    ("B2", "Door2", "Door2-1", "192.168.100.201", "IN"),
    ("B2", "Door2", "Door2-2", "192.168.100.202", "IN"),
    ("B2", "Door2", "Door2-3", "192.168.100.203", "IN"),
    ("B2", "Door2", "Door2-4", "192.168.100.204", "OUT"),
    ("B2", "Door2", "Door2-5", "192.168.100.205", "OUT"),
    ("B2", "Door2", "Door2-6", "192.168.100.206", "OUT"),
]
# Till reader -> the seller it belongs to.
TILL_READERS = [
    ("Reader1", "Demo Cafe"),
    ("Reader2", "Demo Tech Shop"),
    ("Reader3", "Outdoor Playground"),
    ("Reader4", "Demo Bakery"),
]
CARD_ASSIGN_READERS = [("Master1", "Front desk"), ("Master2", "Office")]


def ensure_buildings() -> dict[str, Building]:
    return {
        code: Building.objects.get_or_create(code=code, defaults={"name": name})[0]
        for code, name in BUILDINGS
    }


def ensure_door_units() -> list[str]:
    """Create the door units that don't exist yet; returns the names created."""
    buildings = ensure_buildings()
    created = []
    for bcode, door_id, name, ip, direction in DOOR_UNITS:
        if RFIDDevice.objects.filter(code=door_id, allowed_ip=ip).exists():
            continue
        rfid.register_device(
            actor=None,
            code=door_id,
            name=name,
            location=buildings[bcode].name,
            building=buildings[bcode],
            allowed_ip=ip,
            direction=direction,
        )
        created.append(name)
    return created


def doors_by_building() -> dict[int, dict[str, list[RFIDDevice]]]:
    """{building id: {"IN": [units], "OUT": [units]}} of the active door units."""
    doors: dict[int, dict[str, list[RFIDDevice]]] = {}
    for unit in RFIDDevice.objects.filter(
        purpose=DevicePurpose.ATTENDANCE, is_active=True, building__isnull=False
    ).order_by("name"):
        sides = doors.setdefault(unit.building_id, {"IN": [], "OUT": []})
        for direction in ("IN", "OUT"):
            if unit.direction in (direction, "BOTH"):
                sides[direction].append(unit)
    return {b: sides for b, sides in doors.items() if sides["IN"] and sides["OUT"]}


def ensure_readers() -> list[str]:
    """Till readers of the demo sellers that exist, and the card assign readers."""
    created = []
    for code, seller_name in TILL_READERS:
        seller = Seller.objects.filter(name=seller_name).first()
        if seller is None:
            continue
        reader = RFIDDevice.objects.filter(code=code).first()
        if reader is None:
            rfid.register_device(
                actor=None,
                code=code,
                name=f"{seller_name} till",
                purpose=DevicePurpose.TILL,
                seller=seller,
            )
            created.append(code)
        elif reader.purpose == DevicePurpose.TILL and reader.seller_id is None:
            reader.seller = seller
            reader.save(update_fields=["seller", "updated_at"])
    for code, name in CARD_ASSIGN_READERS:
        if not RFIDDevice.objects.filter(code=code).exists():
            rfid.register_device(actor=None, code=code, name=name, purpose=DevicePurpose.ENROLL)
            created.append(code)
    return created


def link_building_users() -> None:
    """The demo building owner and building manager look after Building 1."""
    b1 = ensure_buildings()["B1"]
    for username, relation in (("building_owner", b1.owners), ("building_manager", b1.managers)):
        user = User.objects.filter(username=username).first()
        if user is not None:
            relation.add(user)
