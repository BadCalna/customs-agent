"""Safe failure codes. Never interpolate provider output or request fields."""

MESSAGES = {
    "AUTH_REJECTED": "服务拒绝鉴权，请检查凭证与授权。",
    "ENDPOINT_UNREACHABLE": "无法连接已登记的端点。",
    "ENDPOINT_NOT_ALLOWED": "端点、模型或鉴权方式未获运维批准。",
    "PROVIDER_LIMIT": "服务触发限频或额度限制。",
    "PROVIDER_REJECTED": "服务拒绝固定调用协议（包括结构化输出）。",
    "TIMEOUT": "调用超时；是否送达或计费未知。",
    "RESPONSE_TOO_LARGE": "响应超出上限，未保留原文。",
    "OUTPUT_SCHEMA_INVALID": "响应不符合结构化契约，或引用了未知材料。",
    "REFERENCE_NOT_READY": "资料未装载，或当前快照尚不可用。",
    "PREVIEW_STALE": "预览已过期或配置、资料发生变化，请重新预览。",
    "WORKER_INTERRUPTED": "执行器中断；已开始的发送结果可能未知，不会自动重发。",
    "QUEUE_EXPIRED": "等待执行器超时，未发送请求。",
    "RUN_DEADLINE": "已超过单次运行期限，不再发送。",
    "CONNECTION_NOT_READY": "没有可用的调试默认连接。",
    "CONNECTION_REVOKED": "连接已紧急撤销，不再发送新请求。",
    "PROBE_REQUIRED": "启用前须在 24 小时内成功测试当前版本。",
    "DEFAULT_CHANGED": "默认连接版本已经变化，请刷新后重试。",
    "SECRET_UNAVAILABLE": "凭证或密钥托管不可用，未发送请求。",
    "DATA_UNAVAILABLE": "受保护的数据不可用。",
    "EXTERNAL_NOT_APPROVED": "未批准外发此精确请求；请改用获批本机连接或模拟。",
    "EXTERNAL_CONFIRMATION_REQUIRED": "须逐次确认向外部服务发送此预览。",
    "RATE_LIMITED": "已达每小时调试与测试上限。",
    "CONCURRENCY_LIMIT": "已有当前用户的调试作业，或实例作业已满。",
    "IDEMPOTENCY_CONFLICT": "幂等键已用于不同的请求。",
    "INVALID_INPUT": "请求字段、大小或格式不符合限制。",
    "INTERNAL_ERROR": "调试操作未完成；细节已隐藏。",
    "unauthorized": "未登录或会话已失效。",
    "forbidden": "无权执行此操作。",
    "not_found": "对象不存在或不可见。",
    "csrf_failed": "CSRF 校验失败，请刷新页面后重试。",
}


class DebugError(Exception):
    def __init__(self, code: str, status: int = 409, *, http_status=None, received=False, truncated=False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.http_status = http_status
        self.received = received
        self.truncated = truncated
