# -*- coding: utf-8 -*-
"""Moonshot（Kimi）限流 / 额度 / 模型权限诊断工具。

用途：升级组织 RPM 后仍遇到 429 时，确认到底卡在哪一层——组织/项目 RPM、余额、
还是某个模型没有权限。全部使用 requests 直连，不需要 openai SDK。

用法（在项目根目录）：
    set MOONSHOT_API_KEY=sk-...
    python tools/moonshot_doctor.py                       # 检测当前 THESIS_MODEL / 默认模型
    python tools/moonshot_doctor.py --model kimi-k2.6 --model kimi-k2.5

检查项：
    1. GET  /v1/models              当前 Key 可用模型列表
    2. GET  /v1/users/me/balance    账户余额/代金券
    3. POST /v1/chat/completions    最小 chat 请求（1 token），打印状态码/耗时/错误原文，
                                    并在响应带限流头时打印出来
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

try:  # pragma: no cover
    import requests
except Exception:  # noqa: BLE001
    requests = None  # type: ignore[assignment]

DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MODEL = "kimi-k2-0905-preview"


def _base() -> str:
    return (os.getenv("MOONSHOT_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def _key() -> str:
    key = (os.getenv("MOONSHOT_API_KEY") or "").strip()
    if not key:
        print("❌ 未设置 MOONSHOT_API_KEY，无法诊断。请在 GUI 填写密钥，或先 set MOONSHOT_API_KEY=sk-...")
        raise SystemExit(2)
    return key


def _headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Accept": "application/json"}


def _short(text: Any, limit: int = 500) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[:limit] + "…"


def check_models(base: str, key: str, timeout: float) -> list[str]:
    print(f"\n[1/3] 模型列表：GET {base}/models")
    if requests is None:
        print("❌ 缺少 requests 包：python -m pip install -r requirements.txt")
        return []
    try:
        response = requests.get(f"{base}/models", headers=_headers(key), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 请求失败（网络错误）：{exc}")
        return []
    print(f"    HTTP {response.status_code}，耗时 {response.elapsed.total_seconds():.2f}s")
    if response.status_code >= 400:
        print(f"    错误原文：{_short(response.text)}")
        return []
    try:
        data = response.json()
    except ValueError:
        print(f"    返回非 JSON：{_short(response.text)}")
        return []
    models = [
        str(item.get("id"))
        for item in (data.get("data") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    print(f"    可用模型 {len(models)} 个：{'、'.join(models[:20])}{'…' if len(models) > 20 else ''}")
    return models


def check_balance(base: str, key: str, timeout: float) -> None:
    print(f"\n[2/3] 账户额度：GET {base}/users/me/balance")
    if requests is None:
        return
    try:
        response = requests.get(f"{base}/users/me/balance", headers=_headers(key), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 请求失败（网络错误）：{exc}")
        return
    print(f"    HTTP {response.status_code}，耗时 {response.elapsed.total_seconds():.2f}s")
    if response.status_code >= 400:
        print(f"    错误原文：{_short(response.text)}")
        return
    try:
        data = response.json()
    except ValueError:
        print(f"    返回非 JSON：{_short(response.text)}")
        return
    payload = data.get("data") if isinstance(data, dict) else None
    payload = payload if isinstance(payload, dict) else (data if isinstance(data, dict) else {})
    print(
        "    可用余额：{}；现金：{}；代金券：{}".format(
            payload.get("available_balance", "—"),
            payload.get("cash_balance", "—"),
            payload.get("voucher_balance", "—"),
        )
    )


def check_chat(base: str, key: str, model: str, timeout: float) -> bool:
    print(f"\n[3/3] 最小 chat 请求：POST {base}/chat/completions（model={model}）")
    if requests is None:
        return False
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
        "stream": False,
    }
    started = time.time()
    try:
        response = requests.post(
            f"{base}/chat/completions", headers={**_headers(key), "Content-Type": "application/json"},
            json=payload, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 请求失败（网络错误）：{exc}")
        return False
    elapsed = time.time() - started
    print(f"    HTTP {response.status_code}，耗时 {elapsed:.2f}s")
    rate_headers = {
        name: value
        for name, value in response.headers.items()
        if "ratelimit" in name.lower() or "retry" in name.lower() or "limit" in name.lower()
    }
    if rate_headers:
        print("    限流相关响应头：" + json.dumps(rate_headers, ensure_ascii=False))
    if response.status_code == 200:
        print("    ✅ chat 调用成功——该模型可用；若 GUI 仍 429，请重启 GUI（旧进程还在用旧退避策略）")
        return True
    print(f"    错误原文：{_short(response.text, 800)}")
    if response.status_code == 429:
        print(
            "    ⚠️ 仍被限流：若刚提高 RPM，请等 60 秒再测；若持续出现且限额显示为 3，"
            "说明组织/项目 RPM 配额尚未生效（在 Moonshot 控制台确认项目级配额）。"
        )
    elif response.status_code == 404:
        print("    ⚠️ 该模型无权限/不存在：把上面的可用模型列表填到 GUI「Agent 模型」再试。")
    elif response.status_code in (401, 403):
        print("    ⚠️ 密钥无效或无权限：检查 GUI 顶部 API Key。")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Moonshot 限流/额度/模型权限诊断")
    parser.add_argument("--model", action="append", default=[], help="要测试的模型（可重复）")
    parser.add_argument("--base-url", default="", help="覆盖 API 根地址（默认取 MOONSHOT_BASE_URL）")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    if args.base_url:
        os.environ["MOONSHOT_BASE_URL"] = args.base_url
    base = _base()
    key = _key()
    models_to_test = [item for item in args.model if item.strip()]
    if not models_to_test:
        env_model = (os.getenv("THESIS_MODEL") or "").strip()
        models_to_test = [env_model or DEFAULT_MODEL]

    print("=" * 72)
    print("Moonshot 限流诊断")
    print(f"API 根地址：{base}")
    print(f"待测模型：{'、'.join(models_to_test)}")
    print("=" * 72)

    available = check_models(base, key, args.timeout)
    check_balance(base, key, args.timeout)

    ok = True
    for model in models_to_test:
        ok = check_chat(base, key, model, args.timeout) and ok

    print("\n" + "=" * 72)
    if ok:
        print("结论：当前 Key 与待测模型可用。若 GUI 仍 429：请完全关闭并重启 GUI（新版本徽标 v2.4），再等 60 秒后重试。")
    else:
        print("结论：存在失败项，请按上面每项的输出处理；把本输出发给维护者可继续定位。")
    print("=" * 72)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
