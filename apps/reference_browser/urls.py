from django.urls import path

from .pages import browser, evidence

app_name = "reference_browser"

urlpatterns = [
    path("", browser, name="index"),
    path("evidence", evidence, name="evidence"),
]
