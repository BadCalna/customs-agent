"""03 基础数据中心交给 01 系统管理装载的信任清单。

只声明元数据，不导入任何业务模型：01 的 ``load_manifests`` 会核对这里的权限码、
事件名、导航路径和 URLConf，装载期间不得触碰参考数据。
"""

PERMISSIONS = (
    "reference_browser.view_catalog",
    "reference_browser.view_history",
    "reference_browser.view_history_detail",
)
# 打开导航只需要看目录；历史案例与详情另外授权。
NAV_PERMISSIONS = ("reference_browser.view_catalog",)
# 与 apps/reference_browser/audit.py 实际发出的 ``reference_browser.<operation>`` 一致。
EVENT_TYPES = (
    "reference_browser.snapshots.read",
    "reference_browser.catalog.read",
    "reference_browser.history.search",
    "reference_browser.history.export",
    "reference_browser.history.detail",
    "reference_browser.evidence.read",
)


def installer():
    return {
        "module_key": "reference_browser",
        "label": "基础数据中心",
        "nav_path": "/ops/data/",
        "urlconf": "apps.reference_browser.urls",
        "install_version": "1",
        "permissions": list(PERMISSIONS),
        "nav_permissions": list(NAV_PERMISSIONS),
        "event_types": list(EVENT_TYPES),
        "api_router": "apps.reference_browser.api.router",
    }