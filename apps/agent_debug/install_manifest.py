"""02 Agent 调试台交给 01 系统管理装载的信任清单。

只声明元数据，不导入调试台模型或配置：01 装载清单时不得读取端点、密钥或资料路径。
"""

PERMISSIONS = (
    "agent_debug.use_debug",
    "agent_debug.manage_connections",
    "agent_debug.view_all_runs",
    "agent_debug.view_trace_payload",
)
# 看到入口只需要使用权限；连接维护、跨人摘要、脱敏报文都另行授权。
NAV_PERMISSIONS = ("agent_debug.use_debug",)
# 与 apps/agent_debug/maintenance.py 的 ``event()`` 调用点一致。
EVENT_TYPES = (
    "debug.connection.created",
    "debug.connection.activated",
    "debug.connection.revoked",
    "debug.probe.queued",
    "debug.probe.finished",
    "debug.run.queued",
    "debug.run.finished",
    "debug.request.rejected",
    "debug.trace.viewed",
    "debug.cleanup.finished",
)


def installer():
    return {
        "module_key": "agent_debug",
        "label": "Agent 调试台",
        "nav_path": "/ops/debug/",
        "urlconf": "apps.agent_debug.urls",
        "install_version": "1",
        "permissions": list(PERMISSIONS),
        "nav_permissions": list(NAV_PERMISSIONS),
        "event_types": list(EVENT_TYPES),
        "api_router": "apps.agent_debug.api.router",
    }