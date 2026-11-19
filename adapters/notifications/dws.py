"""只提供离线 mock；没有 HTTP、密钥、真实人员映射或真实发送路径。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class MockReceipt:
    delivery_id: str
    receipt: str
    mode: str = "mock"


class MockDwsTransport:
    def send(self, *, delivery_id: str, recipient_id: str, title: str, body: str, target_url: str | None):
        # 稳定令牌让重放得到同一 mock receipt；不代表外部通道的幂等承诺。
        return MockReceipt(delivery_id=delivery_id, receipt=f"mock:{delivery_id}")
