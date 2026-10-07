"""DJANGO_ENV_FILE: a settings file named by path is read like the .env file."""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PRINT_SETTINGS = (
    "import django; django.setup(); from django.conf import settings; "
    "print(settings.TIME_ZONE, settings.BACKUP_REQUIRE_PITR, settings.MEDIA_ROOT)"
)


def run_with(env_file: Path, **extra) -> str:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DJANGO_", "TIME_ZONE", "BACKUP_", "MEDIA_ROOT"))
    }
    env |= {
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "DJANGO_ENV_FILE": str(env_file),
        **extra,
    }
    done = subprocess.run(
        [sys.executable, "-c", PRINT_SETTINGS],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def test_settings_are_read_from_the_named_file(tmp_path):
    env_file = tmp_path / "backend.env"
    env_file.write_text(
        "DJANGO_SECRET_KEY=from-the-env-file-long-enough-for-hs256-signing\n"
        "DATABASE_URL=postgres://u:p@localhost:5432/db\n"
        "TIME_ZONE=Asia/Seoul\n"
        "BACKUP_REQUIRE_PITR=false\n"
        "MEDIA_ROOT=/srv/data/media\n"
    )
    assert run_with(env_file) == "Asia/Seoul False /srv/data/media"


def test_the_process_environment_wins_over_the_file(tmp_path):
    env_file = tmp_path / "backend.env"
    env_file.write_text(
        "DJANGO_SECRET_KEY=from-the-env-file-long-enough-for-hs256-signing\n"
        "DATABASE_URL=postgres://u:p@localhost:5432/db\n"
        "TIME_ZONE=Asia/Seoul\n"
    )
    assert run_with(env_file, TIME_ZONE="Europe/Berlin").startswith("Europe/Berlin ")
