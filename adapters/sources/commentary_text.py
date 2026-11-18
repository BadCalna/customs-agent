"""品目注释正文的拼接规则（P23 导入与读取共用）。

为什么单独一个模块：导入流水线（纯标准库）与读取适配（依赖 Django Ninja 的 Schema）
都要用同一条拼接规则来核对 ``sections`` 与 ``text_all``。放在任何一边都会把另一边的
依赖拖进来（例如让 CLI 在 Django settings 就绪前导入 ninja），所以规则独立成一层。

规则与来源一致：每段写成 ``【标签】\\n正文``，段与段之间空一行。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def render_commentary_text(sections: Iterable[Mapping[str, Any] | Any]) -> str:
    """把分段正文拼回全文；``sections`` 可以是字典，也可以是带 ``label``/``text`` 的对象。"""

    blocks: list[str] = []
    for section in sections:
        if isinstance(section, Mapping):
            label = str(section.get("label") or "")
            text = str(section.get("text") or "")
        else:
            label = str(getattr(section, "label", "") or "")
            text = str(getattr(section, "text", "") or "")
        blocks.append(f"【{label}】\n{text}")
    return "\n\n".join(blocks)


__all__ = ["render_commentary_text"]
