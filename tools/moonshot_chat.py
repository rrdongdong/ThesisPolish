# -*- coding: utf-8 -*-
"""Moonshot（Kimi）OpenAI 兼容 chat 的轻量客户端（requests，不依赖 openai SDK）。

背景：openai SDK 3.x（依赖 httpx2）在部分 Windows/Python 环境存在响应解压不兼容：

    TypeError: process() takes no keyword arguments

因此旧版脚本（generate_outline.py / polish.py）改为直接用 requests 调用
``POST {base}/chat/completions``。pi agent 运行时（Node）不受此问题影响。

环境变量：
    MOONSHOT_API_KEY   必填，Kimi API 密钥
    MOONSHOT_BASE_URL  可选，默认 https://api.moonshot.cn/v1（测试可指向本地 mock）
    THESIS_MODEL       可选，默认模型
"""

from __future__ import annotations

import os
import time
from typing import Any

try:  # pragma: no cover - 仅在依赖缺失的环境触发
    import requests
except Exception:  # noqa: BLE001
    requests = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MODEL = "kimi-k2-0905-preview"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_LIMIT = 6


class MoonshotChatError(RuntimeError):
    """可预期的、需要以中文展示给用户的失败。"""


def api_base() -> str:
    return (os.getenv("MOONSHOT_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def api_key() -> str:
    key = (os.getenv("MOONSHOT_API_KEY") or "").strip()
    if not key:
        raise MoonshotChatError(
            "缺少环境变量 MOONSHOT_API_KEY：请先在 GUI 中填写 Kimi API 密钥，"
            "或先在终端执行 set MOONSHOT_API_KEY=sk-..."
        )
    return key


def default_model() -> str:
    return (os.getenv("THESIS_MODEL") or "").strip() or DEFAULT_MODEL


def chat_completion(
    model: str | None,
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.3,
    max_attempts: int = RETRYABLE_LIMIT,
    retry_base: float = 2.0,
    timeout: float = 180.0,
) -> str:
    """单次 chat completion（非流式），返回纯文本。

    * 429 / 5xx / 网络错误自动指数退避重试（``retry_base`` 秒起，最长 60s）；
    * 其它 HTTP 错误直接抛 :class:`MoonshotChatError`（含响应片段）。
    """
    if requests is None:
        raise MoonshotChatError(
            "缺少 requests 包：请执行 python -m pip install -r requirements.txt"
        )
    url = f"{api_base()}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": (model or "").strip() or default_model(),
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    delay = max(float(retry_base), 0.01)
    last_error = ""
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 网络/超时统一重试
            last_error = f"网络错误：{exc}"
            if attempt >= max_attempts:
                break
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
            continue

        if response.status_code in RETRYABLE_STATUS:
            last_error = f"HTTP {response.status_code}: {response.text[:300]}"
            if attempt >= max_attempts:
                break
            time.sleep(delay)
            delay = min(delay * 2, 60.0)
            continue
        if response.status_code >= 400:
            raise MoonshotChatError(
                f"模型调用失败（HTTP {response.status_code}）：{response.text[:500]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise MoonshotChatError(f"模型返回非 JSON：{response.text[:300]}") from exc
        choices = data.get("choices") if isinstance(data, dict) else None
        content = ""
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
        if not str(content).strip():
            raise MoonshotChatError("API 返回空内容（可能是模型拒绝或额度问题）")
        return str(content)

    raise MoonshotChatError(
        f"模型调用失败（已重试 {max_attempts} 次）：{last_error or '未知错误'}"
    )
