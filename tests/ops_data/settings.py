"""Settings used by the independent 03 contract suite.

This module intentionally imports the repository settings only to preserve the
same authentication and database model, then adds reference_browser locally;
no shared settings file is changed by the 03 delivery.
"""
from config.settings import *  # noqa: F401,F403

INSTALLED_APPS = [*INSTALLED_APPS, "apps.reference_browser"]
REFERENCE_BROWSER_FIXTURE_TESTING = True
REFERENCE_BROWSER_FIXTURE = False
ROOT_URLCONF = "tests.ops_data.urls"
