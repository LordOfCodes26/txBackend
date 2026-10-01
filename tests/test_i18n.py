"""Multi-language API: English by default, Korean (DPRK usage) with Accept-Language: ko."""

import ast
import json
import pathlib
import re

import pytest
from django.conf import settings
from django.utils import translation

from apps.developers.models import Developer
from apps.finance.exceptions import InsufficientBalance
from apps.rfid import services as rfid
from apps.rfid.models import Building, RFIDCard, RFIDCardAssignment
from apps.rfid.tcp import handle_frame
from common import i18n_extra
from common.exceptions import DomainError

PO_FILE = pathlib.Path(settings.BASE_DIR) / "locale/ko_KP/LC_MESSAGES/django.po"
ME = "/api/v1/auth/me/"
DEVELOPERS = "/api/v1/developers/"


def _catalog() -> dict[str, str]:
    """msgid -> msgstr (first plural form) from the .po file."""
    text = PO_FILE.read_text()
    entries = {}
    for block in text.split("\n\n"):
        msgid = re.search(r'^msgid (".*")$', block, re.M)
        msgstr = re.search(r'^msgstr(?:\[0\])? (".*")$', block, re.M)
        if msgid and msgstr and not block.lstrip().startswith("#~"):
            key = ast.literal_eval(msgid.group(1))
            if key:
                entries[key] = ast.literal_eval(msgstr.group(1))
    return entries


def test_every_message_is_translated_and_compiled():
    """Every .po entry has a Korean text, and the compiled .mo matches the .po."""
    catalog = _catalog()
    assert len(catalog) > 150
    with translation.override("ko-kp"):
        for msgid, msgstr in catalog.items():
            assert msgstr, f"untranslated: {msgid!r}"
            assert translation.gettext(msgid) == msgstr, (
                f".mo is stale; run compilemessages ({msgid!r})"
            )


def test_placeholders_survive_translation():
    for msgid, msgstr in _catalog().items():
        for pattern in (r"%\(\w+\)[sd]", r"\{\w+\}"):
            assert sorted(re.findall(pattern, msgid)) == sorted(re.findall(pattern, msgstr)), msgid


def test_dprk_spelling():
    """DPRK usage: no initial-sound rule (리용, not 이용), 오유 (not 오류), 수자/날자."""
    text = " ".join(_catalog().values())
    for south, north in [
        ("이용", "리용"),
        ("오류", "오유"),
        ("연결", "련결"),
        ("숫자", "수자"),
        ("날짜", "날자"),
    ]:
        assert south not in text, f"use {north} instead of {south}"


def test_framework_messages_still_exist_upstream():
    """Our overrides only work while DRF / simplejwt keep these exact source strings."""
    import rest_framework
    import rest_framework_simplejwt

    for package, messages in [
        (rest_framework, i18n_extra.DRF),
        (rest_framework_simplejwt, i18n_extra.SIMPLEJWT),
    ]:
        root = pathlib.Path(package.__file__).parent
        source = "".join(p.read_text(errors="ignore") for p in root.rglob("*.py"))
        for message in messages:
            assert message in source or message.replace('"', '\\"') in source, message


def test_domain_error_message_follows_active_language():
    with translation.override("ko-kp"):
        assert str(InsufficientBalance().detail) == "개발자계정의 잔고가 부족합니다."
    assert str(InsufficientBalance().detail) == "Developer account has insufficient balance."
    assert DomainError.code == "DOMAIN_ERROR"


# --- HTTP ------------------------------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("header", ["ko", "ko-KP", "ko-KR,ko;q=0.9,en;q=0.8"])
def test_korean_errors_keep_their_codes(api_client, header):
    response = api_client.get(ME, HTTP_ACCEPT_LANGUAGE=header)
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "NOT_AUTHENTICATED",
        "message": "인증정보가 제공되지 않았습니다.",
    }
    assert response["Content-Language"] == "ko-kp"


@pytest.mark.django_db
def test_english_without_header(api_client):
    response = api_client.get(ME)
    assert response.json()["error"]["message"] == "Authentication credentials were not provided."
    assert response["Content-Language"] == "en"


@pytest.mark.django_db
def test_validation_details_in_korean(auth_client, boss):
    response = auth_client(boss).post(DEVELOPERS, {}, format="json", HTTP_ACCEPT_LANGUAGE="ko")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"] == "입력자료가 옳지 않습니다."
    assert error["details"]["employee_number"] == ["이 항목은 반드시 입력하여야 합니다."]


# --- Devices ---------------------------------------------------------------------------------


@pytest.fixture
def door(db, settings):
    settings.ATTENDANCE_DIRECTION_RULE = "device"
    building = Building.objects.create(code="B1", name="Building 1")
    door, key = rfid.register_device(
        actor=None, code="Door1", building=building, allowed_ip="10.20.0.11"
    )
    dev = Developer.objects.create(employee_number="E1", full_name="김철")
    RFIDCardAssignment.objects.create(card=RFIDCard.objects.create(uid="04A2B3C4"), developer=dev)
    return door, key


def test_device_texts_use_device_language_over_tcp(door, settings):
    settings.DEVICE_LANGUAGE = "ko-kp"
    frame = json.dumps({"ID": "Door1", "Type": "in", "UID": "04A2B3C4"}).encode()
    assert handle_frame(frame, "10.20.0.11")["message"] == "어서 오십시오, 김철"
    unknown = json.dumps({"ID": "Door1", "Type": "in", "UID": "04FFFFFF"}).encode()
    assert handle_frame(unknown, "10.20.0.11")["message"] == "등록되지 않은 카드"
    assert handle_frame(b"not json", "10.20.0.11")["error"] == "프레임이 옳은 JSON이 아닙니다."


def test_device_texts_default_to_english_over_tcp(door):
    frame = json.dumps({"ID": "Door1", "Type": "out", "UID": "04A2B3C4"}).encode()
    assert handle_frame(frame, "10.20.0.11")["message"] == "Goodbye, 김철"


def test_device_language_ignores_accept_language_over_http(api_client, door, settings):
    settings.DEVICE_LANGUAGE = "ko-kp"
    api_client.credentials(HTTP_AUTHORIZATION=f"Device {door[1]}")
    response = api_client.post(
        "/api/v1/rfid/events/", {"uid": "04A2B3C4", "type": "out"}, HTTP_ACCEPT_LANGUAGE="en"
    )
    assert response.status_code == 201, response.json()
    assert response.json()["display_message"] == "안녕히 가십시오, 김철"
