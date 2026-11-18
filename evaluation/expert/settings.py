"""Isolated Django settings for P18 expert-check evaluation; never imports .env or config.settings."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = False
ALLOWED_HOSTS = []
INSTALLED_APPS = []
DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
USE_TZ = True
TIME_ZONE = "UTC"
ENGINE_MODE = "mock"
