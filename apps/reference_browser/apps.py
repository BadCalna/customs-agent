from django.apps import AppConfig
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def reference_browser_deploy_check(app_configs, **kwargs):
    if (getattr(settings, "REFERENCE_BROWSER_FIXTURE", False) or
            getattr(settings, "REFERENCE_BROWSER_FIXTURE_TESTING", False)):
        return [Error("基础数据中心的合成样例不得用于生产环境。", id="reference_browser.E001")]
    return []


class ReferenceBrowserConfig(AppConfig):
    name = "apps.reference_browser"
    default_auto_field = "django.db.models.BigAutoField"
    verbose_name = "基础数据中心（只读参考）"
