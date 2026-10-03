"""Settings templates must parse as written: django-environ keeps a `# comment` after a
value as part of the value (it broke setup-dev.sh once: ATTENDANCE_DIRECTION_RULE)."""

import pathlib
import re

import pytest
from django.conf import settings

TEMPLATES = [".env.example", "deploy/backend.env.template", "deploy/windows/backend.env.template"]


@pytest.mark.parametrize("name", TEMPLATES)
def test_no_comments_after_values(name):
    path = pathlib.Path(settings.BASE_DIR) / name
    bad = [
        line
        for line in path.read_text().splitlines()
        if re.match(r"^[A-Z_]+=", line) and re.search(r"\s#", line)
    ]
    assert bad == [], f"{name}: put comments on their own line: {bad}"
