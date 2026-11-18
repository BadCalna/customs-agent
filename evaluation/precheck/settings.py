"""Minimal offline settings: no dotenv, business apps, secret generation or DB."""
import os

from django.core.exceptions import ImproperlyConfigured

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY.strip():
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY explicitly for offline evaluation.")
INSTALLED_APPS = []
DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
USE_TZ = True
