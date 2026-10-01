"""Races against real Postgres: each thread gets its own DB connection."""

import threading

import pytest
from django.db import connection

from apps.developers.models import Developer
from apps.rfid import services
from apps.rfid.exceptions import RFIDConflict
from apps.rfid.models import RFIDCard, RFIDCardAssignment, RFIDEvent

pytestmark = pytest.mark.django_db(transaction=True)


def run_concurrently(*funcs):
    barrier = threading.Barrier(len(funcs))
    outcomes = [None] * len(funcs)

    def worker(i, fn):
        try:
            barrier.wait()
            outcomes[i] = fn()
        except Exception as exc:  # noqa: BLE001 - we want to inspect every failure
            outcomes[i] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i, f)) for i, f in enumerate(funcs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def make_dev(n):
    return Developer.objects.create(employee_number=f"E{n}", full_name=f"D{n}")


def test_concurrent_assignment_of_one_card_to_two_developers():
    card = RFIDCard.objects.create(uid="04AA0001")
    a, b = make_dev(1), make_dev(2)

    outcomes = run_concurrently(
        lambda: services.assign_card(actor=None, card=card, developer=a),
        lambda: services.assign_card(actor=None, card=card, developer=b),
    )

    assert sum(isinstance(o, RFIDCardAssignment) for o in outcomes) == 1
    assert sum(isinstance(o, RFIDConflict) for o in outcomes) == 1
    assert RFIDCardAssignment.objects.filter(unassigned_at__isnull=True).count() == 1


def test_concurrent_assignment_of_two_cards_to_one_developer():
    c1, c2 = RFIDCard.objects.create(uid="04AA0001"), RFIDCard.objects.create(uid="04AA0002")
    dev = make_dev(1)

    outcomes = run_concurrently(
        lambda: services.assign_card(actor=None, card=c1, developer=dev),
        lambda: services.assign_card(actor=None, card=c2, developer=dev),
    )

    assert sum(isinstance(o, RFIDCardAssignment) for o in outcomes) == 1
    assert RFIDCardAssignment.objects.filter(developer=dev).count() == 1


def test_simultaneous_scans_on_two_readers_accept_once():
    card = RFIDCard.objects.create(uid="04AA0001")
    RFIDCardAssignment.objects.create(card=card, developer=make_dev(1))
    r1, _ = services.register_device(actor=None, code="R1")
    r2, _ = services.register_device(actor=None, code="R2")

    outcomes = run_concurrently(
        lambda: services.record_scan(device=r1, uid=card.uid),
        lambda: services.record_scan(device=r2, uid=card.uid),
    )

    assert all(isinstance(o, tuple) for o in outcomes), outcomes
    results = sorted(RFIDEvent.objects.values_list("result", flat=True))
    assert results == ["ACCEPTED", "DUPLICATE"]


def test_concurrent_retries_store_one_event():
    card = RFIDCard.objects.create(uid="04AA0001")
    RFIDCardAssignment.objects.create(card=card, developer=make_dev(1))
    device, _ = services.register_device(actor=None, code="R1")

    outcomes = run_concurrently(
        *[lambda: services.record_scan(device=device, uid=card.uid, client_event_id="x1")] * 3
    )

    assert all(isinstance(o, tuple) for o in outcomes), outcomes
    assert len({event.pk for event, _ in outcomes}) == 1
    assert RFIDEvent.objects.count() == 1
