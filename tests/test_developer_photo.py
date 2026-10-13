"""A developer's photo: set, replaced, removed, and shown wherever a developer is embedded."""

import io
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.accounts.rbac import Roles
from apps.audit.models import AuditLog
from apps.developers.models import Developer
from apps.developers.serializers import DeveloperSummarySerializer


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


@pytest.fixture
def ada(db):
    return Developer.objects.create(employee_number="E1", full_name="Ada")


def png(name="ada.png", size=(40, 40)):
    data = io.BytesIO()
    Image.new("RGB", size, "teal").save(data, "PNG")
    return SimpleUploadedFile(name, data.getvalue(), content_type="image/png")


def url(developer):
    return f"/api/v1/developers/{developer.pk}/photo/"


def test_set_replace_and_remove_a_photo(
    auth_client, admin, ada, media, django_capture_on_commit_callbacks
):
    client = auth_client(admin)
    r = client.post(url(ada), {"photo": png()}, format="multipart")
    assert r.status_code == 200, r.json()
    first = r.json()["photo"]
    assert first.startswith("/media/developers/") and first.endswith(".png")
    assert "ada" not in first  # a random name, not the uploaded one
    first_file = media / first.removeprefix("/media/")
    assert first_file.exists()
    # It is part of the developer everywhere they are embedded (checks, checkout, ...).
    ada.refresh_from_db()
    assert DeveloperSummarySerializer(ada).data["photo"] == first

    with django_capture_on_commit_callbacks(execute=True):  # old files go after the commit
        second = client.post(url(ada), {"photo": png("new.png")}, format="multipart").json()[
            "photo"
        ]
    assert second != first and not first_file.exists()  # the old file is gone

    with django_capture_on_commit_callbacks(execute=True):
        r = client.delete(url(ada))
    assert r.status_code == 200 and r.json()["photo"] is None
    assert not (media / second.removeprefix("/media/")).exists()
    actions = list(
        AuditLog.objects.filter(action__startswith="developer.photo").values_list(
            "action", flat=True
        )
    )
    assert sorted(actions) == [
        "developer.photo_changed",
        "developer.photo_changed",
        "developer.photo_removed",
    ]


def test_only_images_up_to_the_limit(auth_client, admin, ada, settings):
    client = auth_client(admin)
    text = SimpleUploadedFile("cv.txt", b"not an image", content_type="text/plain")
    assert (
        "photo"
        in client.post(url(ada), {"photo": text}, format="multipart").json()["error"]["details"]
    )
    settings.DEVELOPER_PHOTO_MAX_BYTES = 100
    big = png(size=(400, 400))
    r = client.post(url(ada), {"photo": big}, format="multipart")
    assert "photo" in r.json()["error"]["details"]
    assert not Path(settings.MEDIA_ROOT, "developers").exists()


def test_who_may_change_it(auth_client, make_user, ada):
    viewer = auth_client(make_user(Roles.BOSS))  # developer.view only
    assert viewer.post(url(ada), {"photo": png()}, format="multipart").status_code == 403
    assert viewer.get(f"/api/v1/developers/{ada.pk}/").json()["photo"] is None
    # A normal edit cannot set it (it is read-only there).
    manager = auth_client(make_user(Roles.MANAGER))
    manager.patch(f"/api/v1/developers/{ada.pk}/", {"photo": "x.png"})
    ada.refresh_from_db()
    assert not ada.photo
