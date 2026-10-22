import io

import pytest
from django.core.management import call_command
from django.db.models import Count
from django.db.models.functions import Lower

from apps.rfid.models import RFIDCard


@pytest.mark.django_db
def test_demo_seeds_run_one_after_the_other_with_unique_card_labels(settings):
    """The offline kit's --seed-demo runs these on a development copy. Card labels are
    unique, so seed_more must not reuse the labels of seed_demo's spare cards."""
    settings.DEBUG = True
    call_command("seed_demo", stdout=io.StringIO())
    call_command("seed_more", "--developers", "10", stdout=io.StringIO())
    clashes = (
        RFIDCard.objects.exclude(label="")
        .values(key=Lower("label"))
        .annotate(n=Count("pk"))
        .filter(n__gt=1)
    )
    assert list(clashes) == []
    assert RFIDCard.objects.filter(label="0031").exists()  # seed_more's own card
    assert RFIDCard.objects.filter(label="S031").exists()  # a seed_demo spare
