# -*- coding: utf-8 -*-
"""ThesisPolish 的 pi agent 运行时客户端（stdlib only，Python 3.12，Windows）。

本模块把 pi v0.85.x 以子进程方式嵌入 tkinter GUI：

* **主路径**：``node <pi-cli.js> --mode rpc ...`` 长驻进程，stdin/stdout 走严格
  JSONL 协议（一行一条 JSON）。支持多轮对话、流式 ``text_delta``、工具执行事件、
  取消（``abort``）、新建会话（``new_session``）。
* **降级路径**：``run_once_json()`` 用 ``--mode json`` 一次性进程，正文通过
  **stdin** 合并进 prompt（绝不放进 argv，规避 Windows 约 32K 字符的命令行上限）。

设计要点（详见 ``docs/pi-agent-integration.md``）：

* 用绝对路径的 ``node.exe`` + ``dist/bundle/cli.js`` 启动，避免 ``.cmd`` 经
  cmd.exe 时 ``%VAR%`` 被展开；Windows 上加 ``CREATE_NO_WINDOW`` 不弹黑窗。
* 子进程 env：``PI_CODING_AGENT_DIR=<repo>/.pi/agent``、``PI_OFFLINE=1``、
  ``MOONSHOT_API_KEY``（只走 env，绝不进 argv）、``THESIS_PYTHON=sys.executable``、
  ``PYTHONIOENCODING=utf-8``。provider 用 pi 内置的 ``moonshotai-cn``，
  生产不需要 models.json。
* provider/model 可用环境变量覆盖（无需改代码即可切换更新版 Kimi 模型）：

  - ``THESIS_PROVIDER``：默认 ``moonshotai-cn``（pi 内置 Moonshot 国内 provider）；
  - ``THESIS_MODEL``：默认 ``kimi-k2-0905-preview``（pi 0.85.1 内置模型目录；也可用
    目录中的 ``kimi-k2.5`` / ``kimi-k2.6`` / ``kimi-k3`` 等）。

  两者在模块导入时解析到 ``PROVIDER`` / ``MODEL`` 常量，并可在 ``PiRuntime``
  实例化时重新读取（与 ``AGENT_DIR`` / ``SYSTEM_PROMPT_PATH`` 相同的模式）；
  ``DEFAULT_PROVIDER`` / ``DEFAULT_MODEL`` 仍保留为契约中的默认值常量。
* stdout 用**二进制**行读取线程：只按 ``b"\\n"`` 切行、去掉尾部 ``\\r``、
  UTF-8 解码；事件与 ``response`` 在同一条流里交错出现，必须逐行分发。
* 命令带 ``id``，``response`` 携带相同 ``id``；``start()`` 只有拿到
  ``get_state`` 的成功响应才算就绪。
* ``stop()`` 不依赖正常退出码：先尽力发 ``abort``，随后 ``terminate()`` /
  ``kill()``，规避 pi 在 Windows 上「关闭 stdin 触发 libuv 断言崩溃」的上游缺陷。
* 所有用户可见错误均为**中文**，并给出可操作建议。

线程模型与回调约定
------------------

* 本类内部有 2~3 个后台线程：RPC stdout 读取线程、stderr 收集线程
  （``run_once_json`` 还会临时启动读写线程）。
* ``on_event`` 回调可能在**读取线程**或调用方线程被触发；tkinter 的宿主
  **必须**只在回调里做线程安全的操作（例如 ``queue.Queue.put``），绝不能在
  回调里直接操作控件。推荐 GUI 用 ``root.after`` 轮询队列。
* ``on_event`` 里抛出的异常会被吞掉并记录日志，不会影响协议读取。
* ``ask()`` / ``abort()`` / ``new_session()`` 立即返回（不等待模型输出）；
  ``start()`` / ``stop()`` / ``run_once_json()`` 是阻塞调用，应在工作线程中调用
  （``start()`` 实测约 0.3~1s；``stop()`` 保证 3 秒内返回）。
"""

from __future__ import annotations

import collections
import concurrent.futures
import itertools
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 常量（与 GUI.py 的契约，不要随意改名）
# --------------------------------------------------------------------------

#: 项目根目录：本文件所在目录。
BASE_DIR: Path = Path(__file__).resolve().parent

#: pi 内置国内 Moonshot provider（https://api.moonshot.cn/v1，读取 MOONSHOT_API_KEY）。
DEFAULT_PROVIDER = "moonshotai-cn"
#: Kimi 模型 id（pi 0.85.x 内置模型目录已包含，262144 上下文）。
DEFAULT_MODEL = "kimi-k2-0905-preview"


def _resolve_provider() -> str:
    """解析 provider：环境变量 ``THESIS_PROVIDER`` 优先，否则 ``DEFAULT_PROVIDER``。"""
    override = os.environ.get("THESIS_PROVIDER", "").strip()
    return override or DEFAULT_PROVIDER


def _resolve_model() -> str:
    """解析模型：环境变量 ``THESIS_MODEL`` 优先，否则 ``DEFAULT_MODEL``。"""
    override = os.environ.get("THESIS_MODEL", "").strip()
    return override or DEFAULT_MODEL


#: 实际使用的 provider（环境变量 ``THESIS_PROVIDER`` 可覆盖，如 ``moonshotai``）。
PROVIDER: str = _resolve_provider()
#: 实际使用的模型（环境变量 ``THESIS_MODEL`` 可覆盖，如 ``kimi-k2.6``）。
MODEL: str = _resolve_model()

#: 授予 agent 的工具白名单：内置只读工具 + 项目扩展（thesis_*）工具。
#: ``--tools`` 是严格白名单；扩展工具由 ``<AGENT_DIR>/extensions/thesis-tools.ts`` 注册。
AGENT_TOOLS = (
    "read,grep,ls,find,"
    "thesis_pdf_ingest,thesis_outline_validate,"
    "thesis_outline_save,thesis_save_polished"
)

#: Windows 下隐藏控制台窗口的创建标志（非 Windows 平台为 0）。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

#: pi npm 包在 node_modules 中的相对路径（相对 npm 全局目录 / APPDATA\\npm）。
_PI_CLI_RELPATH = (
    Path("node_modules")
    / "@earendil-works"
    / "pi-coding-agent"
    / "dist"
    / "bundle"
    / "cli.js"
)

#: 启动握手（spawn -> get_state 成功）的超时时间（秒）。
STARTUP_TIMEOUT = 30.0
#: stop() 中 terminate 之后等待进程退出的时间（秒）；超时后 kill。
_STOP_WAIT = 1.5


def _resolve_agent_dir(base_dir: Path) -> Path:
    """解析 pi 配置目录：环境变量 ``THESIS_PI_AGENT_DIR`` 优先，否则 ``<base_dir>/.pi/agent``。"""
    override = os.environ.get("THESIS_PI_AGENT_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(base_dir) / ".pi" / "agent"


def _resolve_system_prompt(base_dir: Path) -> Path:
    """解析系统提示词文件：环境变量 ``THESIS_SYSTEM_PROMPT`` 优先，否则 ``<base_dir>/.pi/prompts/thesis_system.md``。"""
    override = os.environ.get("THESIS_SYSTEM_PROMPT", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(base_dir) / ".pi" / "prompts" / "thesis_system.md"


#: pi 配置目录（环境变量 ``THESIS_PI_AGENT_DIR`` 可覆盖）。测试用临时目录隔离。
AGENT_DIR: Path = _resolve_agent_dir(BASE_DIR)
#: 系统提示词文件（环境变量 ``THESIS_SYSTEM_PROMPT`` 可覆盖），经 ``--system-prompt`` 传给 pi。
SYSTEM_PROMPT_PATH: Path = _resolve_system_prompt(BASE_DIR)


# --------------------------------------------------------------------------
# 公共数据类型 / 异常
# --------------------------------------------------------------------------


@dataclass
class AgentEvent:
    """pi 运行时事件（GUI 轮询队列里的元素）。

    ``kind`` 取值（与 GUI.py 契约一致）：

    * ``text_delta``：流式正文增量，``text`` 是本次增量；
    * ``tool_start`` / ``tool_update`` / ``tool_end``：工具调用开始/进度/结束，
      ``data`` 含 ``toolName``、``args``、``toolCallId``；结束时含
      ``is_error``、``result_text``；
    * ``status``：阶段信息（启动、提交、重试、用量、停止等），``text`` 可直接展示；
    * ``error``：错误信息（模型失败/进程退出/启动失败等），``text`` 为中文说明；
    * ``done``：一次任务结束，``data = {ok, error, usage, text, aborted}``；
    * ``session``：会话建立/新建，``data`` 含 ``session_id`` 等。
    """

    kind: str
    text: str = ""
    data: dict = field(default_factory=dict)


class PiUnavailable(RuntimeError):
    """pi 运行时环境不可用（找不到 node / pi CLI、无法执行 --version 等）。"""


class PiRuntimeError(RuntimeError):
    """pi 运行时调用失败（启动失败、协议错误、模型调用失败、超时等）。"""


# --------------------------------------------------------------------------
# 文本 / 错误处理工具
# --------------------------------------------------------------------------


def _decode(raw: bytes | str | None) -> str:
    """把子进程输出安全解码为 UTF-8 文本。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", "replace")


def _content_text(content: Any) -> str:
    """从 pi 的 message.content / tool result.content 中提取文本。

    content 可能是字符串，也可能是 ``[{"type": "text", "text": ...}, ...]``
    形式的块列表（也可能混入 toolCall/image 块，忽略非文本块）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        for key in ("text", "content"):
            if isinstance(content.get(key), str):
                return content[key]
        return ""
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "text" and isinstance(block.get("content"), str):
                    parts.append(block["content"])
        return "".join(parts)
    return str(content)


def _summarize(text: str, limit: int = 4000) -> str:
    """截断过长的工具输出/错误文本，避免把整篇论文塞进 GUI 状态栏或事件数据。"""
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"


def humanize_pi_error(raw: Any) -> str:
    """把 pi / 模型返回的英文错误翻译成可操作的中文提示。"""
    text = "" if raw is None else str(raw).strip()
    low = text.lower()
    if not text:
        return "pi 运行出错（错误信息为空）。"
    if "no api key" in low:
        return "pi 未找到 API 密钥（MOONSHOT_API_KEY）。请在 GUI 中填写有效的 Kimi（Moonshot）API 密钥后重试。"
    if "unknown provider" in low:
        return (
            "pi 不认识该 provider。本项目需要 pi 0.85.x（内置 moonshotai-cn provider）；"
            "请升级：npm i -g @earendil-works/pi-coding-agent。\n原始错误：" + _summarize(text, 300)
        )
    if "already processing" in low:
        return "pi 仍在处理上一条任务，请等待完成或点击“停止”后再试。"
    if "request was aborted" in low or low in ("aborted", "abort"):
        return "操作已取消。"
    if "resource_not_found" in low or "not found the model" in low or (
        "404" in low and "model" in low
    ):
        return (
            "当前模型对该 API Key 不可用（404 resource_not_found：模型不存在或无权限）。\n"
            "请在 GUI 顶部「Agent 模型」处点击「刷新模型」，选择一个当前账号可用的模型后重试；"
            "也可以设置环境变量 THESIS_MODEL 指定模型，或用 `pi --list-models kimi` 查看 pi 内置目录。\n"
            "原始错误：" + _summarize(text, 300)
        )
    if any(k in low for k in ("401", "unauthorized", "invalid api key", "authentication", "invalid_api_key")):
        return "API 密钥无效或已过期（401），请检查 Kimi（Moonshot）密钥。"
    if any(k in low for k in ("429", "rate limit", "rate_limit", "quota")):
        rpm_match = re.search(r"max\s*RPM\s*[:：]\s*(\d+)", text, re.IGNORECASE)
        rpm_hint = ""
        if rpm_match:
            rpm_hint = (
                f"检测到当前组织限制为 {rpm_match.group(1)} RPM（每分钟请求数）。"
                "agent 一次大纲任务至少需要 3~4 次模型调用，会周期性地触发限流；"
                "pi 会自动等待重试，也可以改用「传统流程（兼容）」的单次调用流程，"
                "或在 Moonshot 控制台充值/申请提高组织的 RPM 配额。\n"
            )
        return (
            "请求频率或额度受限（429/quota）。pi 已自动重试；若仍失败：\n"
            + rpm_hint
            + "1) 点击 GUI 顶部「查询额度」确认账户余额/代金券是否充足；\n"
            "2) 等待约 1 分钟再试（Moonshot 有每分钟 token/请求限额）；\n"
            "3) 换用较轻量的模型（「刷新模型」重新选择），或减少一次读取的正文量。\n"
            "原始错误：" + _summarize(text, 300)
        )
    if any(k in low for k in ("overloaded", "500", "server_error", "502", "503", "504")):
        return "模型服务暂时不可用（上游错误），请稍后重试。"
    if any(k in low for k in ("timed out", "timeout", "etimedout", "fetch failed", "econnrefused", "enotfound")):
        return "连接模型服务超时或失败，请检查网络、代理设置（HTTP_PROXY/HTTPS_PROXY）后重试。"
    return "pi 运行出错：" + _summarize(text, 500)


def _moonshot_base_url() -> str:
    """Moonshot API 根地址（``MOONSHOT_BASE_URL`` 可覆盖，便于离线测试）。"""
    value = os.environ.get("MOONSHOT_BASE_URL", "").strip()
    return (value or "https://api.moonshot.cn/v1").rstrip("/")


def fetch_moonshot_models(
    api_key: str,
    *,
    base_url: Optional[str] = None,
    timeout: float = 20.0,
) -> list[str]:
    """查询当前 Moonshot（Kimi）账号可用的模型 ID 列表（``GET {base}/models``）。

    用户账号可用的模型与其额度/权限有关（例如可能出现
    ``404 resource_not_found_error: Not found the model ... or Permission denied``），
    因此 GUI 提供「刷新模型」按钮，用同一个 API Key 实时拉取可用列表。

    参数：
        api_key: Kimi/Moonshot API 密钥。
        base_url: 可选，默认取 ``MOONSHOT_BASE_URL`` 或 https://api.moonshot.cn/v1。
        timeout: 网络超时（秒）。

    返回：模型 ID 列表（至少一个）。失败抛 :class:`PiRuntimeError`（中文、可操作）。
    """
    key = (api_key or "").strip()
    if not key:
        raise PiRuntimeError(
            "未提供 Kimi（Moonshot）API 密钥，无法查询可用模型。\n"
            "请在 GUI 顶部「kimi API 密钥」输入框中填写密钥后重试。"
        )
    base = (base_url or _moonshot_base_url()).rstrip("/")
    url = f"{base}/models"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()[:300]
        except Exception:  # noqa: BLE001 - 读取错误体失败不影响主错误
            detail = ""
        suffix = f"：{detail}" if detail else ""
        if exc.code in (401, 403):
            raise PiRuntimeError(
                f"查询模型列表失败（HTTP {exc.code}），API 密钥可能无效或无权限。{suffix}"
            ) from exc
        raise PiRuntimeError(f"查询模型列表失败（HTTP {exc.code}）。{suffix}") from exc
    except Exception as exc:  # noqa: BLE001 - URLError/超时/DNS 等统一转中文
        raise PiRuntimeError(
            f"查询模型列表失败（网络错误）：{exc}\n"
            "请检查网络/代理（HTTP_PROXY/HTTPS_PROXY）后重试。"
        ) from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiRuntimeError(f"模型列表返回格式异常：{_summarize(raw, 200)}") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    models: list[str] = []
    for item in data or []:
        if isinstance(item, dict):
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id.strip():
                models.append(model_id.strip())
    if not models:
        raise PiRuntimeError(
            "模型列表为空：当前 API Key 名下没有可用模型，请确认账号/额度/权限。"
        )
    return models


def fetch_moonshot_balance(
    api_key: str,
    *,
    base_url: Optional[str] = None,
    timeout: float = 20.0,
) -> dict:
    """查询 Moonshot（Kimi）账户余额（``GET {base}/users/me/balance``）。

    返回 ``{"available_balance": float|None, "cash_balance": float|None,
    "voucher_balance": float|None, "raw": dict}``；字段缺失时为 None。
    失败抛 :class:`PiRuntimeError`（中文）。
    """
    key = (api_key or "").strip()
    if not key:
        raise PiRuntimeError(
            "未提供 Kimi（Moonshot）API 密钥，无法查询账户余额。\n"
            "请在 GUI 顶部「kimi API 密钥」输入框中填写密钥后重试。"
        )
    base = (base_url or _moonshot_base_url()).rstrip("/")
    url = f"{base}/users/me/balance"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()[:300]
        except Exception:  # noqa: BLE001
            detail = ""
        raise PiRuntimeError(f"查询余额失败（HTTP {exc.code}）。{detail}".strip()) from exc
    except Exception as exc:  # noqa: BLE001 - 网络/DNS/超时
        raise PiRuntimeError(f"查询余额失败（网络错误）：{exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PiRuntimeError(f"余额接口返回格式异常：{_summarize(raw, 200)}") from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        # 有些账号/网关可能直接返回扁平结构
        data = payload if isinstance(payload, dict) else {}

    def _num(*names: str) -> Optional[float]:
        for name in names:
            value = data.get(name)
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                try:
                    return float(value)
                except ValueError:
                    continue
        return None

    return {
        "available_balance": _num("available_balance", "availableBalance"),
        "cash_balance": _num("cash_balance", "cashBalance"),
        "voucher_balance": _num("voucher_balance", "voucherBalance"),
        "raw": payload if isinstance(payload, dict) else {},
    }


def _extract_text_from_message(message: Any) -> str:
    """提取一条 assistant 消息里的纯文本内容。"""
    if not isinstance(message, dict):
        return ""
    return _content_text(message.get("content"))


def _extract_last_assistant_text(messages: Any) -> str:
    """从 ``agent_end.messages`` 中取最后一条 assistant 消息的文本。"""
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            return _extract_text_from_message(message)
    return ""


def _tool_result_text(result: Any) -> str:
    """把 ``tool_execution_end.result`` / ``tool_execution_update.partialResult`` 转成文本。"""
    if isinstance(result, dict):
        return _content_text(result.get("content"))
    return _content_text(result)


def _new_request_id(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{next(_ID_COUNTER)}"


_ID_COUNTER = itertools.count(1)


# --------------------------------------------------------------------------
# pi CLI / node 发现
# --------------------------------------------------------------------------


def find_node() -> Path:
    """定位 node 可执行文件。

    顺序：环境变量 ``THESIS_NODE`` > ``shutil.which("node")``。
    找不到时抛 :class:`PiUnavailable`（中文、可操作）。
    """
    override = os.environ.get("THESIS_NODE", "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return path.resolve()
        raise PiUnavailable(
            f"环境变量 THESIS_NODE 指定的 node 不存在：{path}\n"
            "请把它改为 node.exe 的实际路径，或删除该变量让程序自动查找。"
        )
    found = shutil.which("node")
    if found:
        return Path(found).resolve()
    raise PiUnavailable(
        "未找到 node.exe。请安装 Node.js（https://nodejs.org/）并确保 node 在 PATH 中，"
        "或设置环境变量 THESIS_NODE 指向 node.exe 的绝对路径。"
    )


def find_pi_cli() -> Path:
    """定位 pi 的 ``cli.js`` 入口。

    顺序：

    1. 环境变量 ``THESIS_PI_CLI``（指向 ``cli.js``；也允许 ``.cmd`` 启动脚本）；
    2. 由 ``shutil.which("pi")`` 所在 npm 目录推导
       ``node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js``；
    3. ``%APPDATA%\\npm\\node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js``。

    找不到时抛 :class:`PiUnavailable`。
    """
    override = os.environ.get("THESIS_PI_CLI", "").strip()
    if override:
        path = Path(override).expanduser()
        if path.is_file():
            return path.resolve()
        raise PiUnavailable(
            f"环境变量 THESIS_PI_CLI 指向的文件不存在：{path}\n"
            "请把它改为 pi 的 cli.js 绝对路径（通常在 "
            "%APPDATA%\\npm\\node_modules\\@earendil-works\\pi-coding-agent\\dist\\bundle\\cli.js），"
            "或删除该变量让程序自动查找。"
        )

    candidates: list[Path] = []
    which_pi = shutil.which("pi")
    if which_pi:
        npm_dir = Path(which_pi).resolve().parent
        candidates.append(npm_dir / _PI_CLI_RELPATH)
        # 有些环境 which("pi") 返回 node_modules/.bin 里的 shim。
        candidates.append(npm_dir.parent / _PI_CLI_RELPATH)
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        candidates.append(Path(appdata) / "npm" / _PI_CLI_RELPATH)

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    hint = (
        "未找到 pi CLI（cli.js）。请先安装 pi：\n"
        "  npm i -g @earendil-works/pi-coding-agent\n"
        "或设置环境变量 THESIS_PI_CLI 指向 "
        "%APPDATA%\\npm\\node_modules\\@earendil-works\\pi-coding-agent\\dist\\bundle\\cli.js。"
    )
    raise PiUnavailable(hint)


def _launch_prefix(node: Path, pi_cli: Path) -> list[str]:
    """构造启动 pi 的命令前缀。

    * 正常情况：``[node.exe, cli.js]``（推荐，绕开 cmd.exe）；
    * 若 ``THESIS_PI_CLI`` 明确给了 ``.cmd/.bat``：直接执行该脚本（由用户负责
      PATH 中有 node），仅作为兼容入口。
    """
    if pi_cli.suffix.lower() in (".cmd", ".bat"):
        return [str(pi_cli)]
    return [str(node), str(pi_cli)]


def detect_pi_version(
    node: Path,
    pi_cli: Path,
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> str:
    """运行 ``<node> <cli> --version`` 并解析版本号（如 ``0.85.1``）。"""
    argv = _launch_prefix(Path(node), Path(pi_cli)) + ["--version"]
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise PiUnavailable(f"无法执行 pi（node 或 cli.js 路径不存在）：{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PiUnavailable(f"检测 pi 版本超时（>{timeout:.0f}s），pi 可能卡死。") from exc
    except OSError as exc:
        raise PiUnavailable(f"无法执行 pi 以检测版本：{exc}") from exc

    stdout = _decode(completed.stdout)
    stderr = _decode(completed.stderr)
    match = re.search(r"(\d+\.\d+\.\d+)", stdout) or re.search(r"(\d+\.\d+\.\d+)", stderr)
    if not match:
        detail = (stdout + "\n" + stderr).strip()[:500] or "（无输出）"
        raise PiUnavailable(
            f"无法从 pi --version 的输出中解析版本号（退出码 {completed.returncode}）。\n"
            f"输出：{detail}"
        )
    return match.group(1)


# --------------------------------------------------------------------------
# Prompt 构造函数（模块级；GUI 直接调用；内容均为中文）
# --------------------------------------------------------------------------


def build_outline_prompt(pdf_path: Path) -> str:
    """构造「PDF -> 大纲」任务的 prompt。

    要求 agent 先用 ``thesis_pdf_ingest`` 把 PDF 解析到 ``.thesis/pdf_text.txt``，
    再用 read 分段阅读正文，产出 9 行严格模板大纲，经 ``thesis_outline_validate``
    校验后用 ``thesis_outline_save`` 落盘 ``outline.txt``；**文件才是交付物**。
    """
    pdf = str(Path(pdf_path))
    return (
        "【任务】从论文 PDF 中提取大纲，并把结果保存为项目文件。\n"
        f"【PDF 路径】{pdf}\n"
        "【执行步骤】\n"
        "1. 先调用工具 thesis_pdf_ingest（参数 path 为上面的 PDF 路径，force 可省略），"
        "把 PDF 正文解析到项目内的 .thesis/pdf_text.txt。若返回成功，不要重复解析。\n"
        "2. 检查 ingest 返回的 JSON：如果 preview 已包含论文题目与完整摘要、headings 已包含各章节标题、"
        "tail_preview 已包含结论/参考文献附近内容，可直接据此填写模板，**不必再调用 read**"
        "（可显著减少模型调用次数，避免低 RPM 账号触发 429）；信息不足时才用 read 读取 "
        ".thesis/pdf_text.txt（第一次 limit=2000，被截断再用 offset 续读）。"
        "只依据正文真实内容提取，禁止编造题目、章节或摘要。\n"
        "3. 严格按下面 9 行模板输出（每行“标签：内容”；正文中确实没有的信息写“未提供”，不要删行）：\n"
        "题目：…\n"
        "摘要：…\n"
        "第一章题目：…\n"
        "第二章题目：…\n"
        "第三章题目：…\n"
        "第四章题目：…\n"
        "第五章题目：…\n"
        "总结：…\n"
        "参考文献：…\n"
        "4. 直接调用 thesis_outline_save 保存（该工具自带九行模板校验，会返回 issues）；"
        "若有 issues，按提示修正内容后再次调用 save，不需要单独调用 thesis_outline_validate。\n"
        "5. 最后用一两句话向我汇报：是否通过校验、保存路径、大纲字符数。\n"
        "【重要】最终交付物是 outline.txt 文件；不要只在聊天回复里给出大纲。"
    )


def build_polish_prompt(position: str, text: str, outline_path: Path) -> str:
    """构造「按大纲润色指定位置段落」任务的 prompt。

    要求 agent 先读大纲（必要时参考 ``.thesis/pdf_text.txt``），只做语言润色
    （语义不变、不新增事实），最后用 ``thesis_save_polished`` 写入 ``polished.txt``。
    """
    outline = str(Path(outline_path))
    return (
        "【任务】按论文大纲润色指定位置的段落，并把结果保存为项目文件。\n"
        f"【润色位置】{position}\n"
        f"【大纲文件】{outline}\n"
        "【待润色原文】\n"
        f"{text}\n"
        "【执行步骤】\n"
        "1. 先用 read 工具读取大纲文件，确认该位置在论文中的上下文；"
        "如需更多依据，用 read（offset/limit 分段）查看 .thesis/pdf_text.txt 中对应章节，"
        "确保术语、章节编号、引用标记与原文一致。\n"
        "2. 只改语言表达：更通顺、更学术化、去口语化、修正病句与标点；"
        "语义不变，不新增事实、数据、实验或参考文献，不删减关键信息。\n"
        "3. 不要输出解释、标题或 Markdown 代码块，只输出润色后的正文文本。\n"
        "4. 调用 thesis_save_polished 工具保存结果（text 为润色后的文本，position 为润色位置），"
        "工具会写入 polished.txt 并记录修改历史。\n"
        "5. 最后用一两句话汇报保存路径与大致字数。\n"
        "【重要】最终交付物是 polished.txt 文件；不要把润色文本只放在聊天回复里。"
    )


def build_chat_prompt(text: str) -> str:
    """构造「Agent 标签页自由对话/问答」的 prompt。"""
    return (
        "【对话】你是 ThesisPolish 的论文润色助手。请回答下面的问题或完成请求。\n"
        "如果涉及论文内容，先用 read 工具读取项目内的 outline.txt 或 .thesis/pdf_text.txt"
        "（分段读取）再回答，不要编造事实、数据或参考文献。\n"
        "需要保存润色结果时使用 thesis_save_polished 工具；"
        "需要更新大纲时使用 thesis_outline_validate + thesis_outline_save 工具。\n"
        f"【用户输入】\n{text}"
    )


# --------------------------------------------------------------------------
# 运行时实现
# --------------------------------------------------------------------------


class PiRuntime:
    """pi RPC 运行时（长驻子进程）。

    典型用法（GUI.py）::

        runtime = PiRuntime(base_dir=BASE_DIR, on_event=lambda ev: q.put(ev))
        ok, reason = runtime.check_available()
        runtime.ask(build_outline_prompt(pdf), api_key=key)   # 立即返回
        # ... root.after 轮询 q 里的 AgentEvent ...
        runtime.abort()        # 用户点“停止”
        runtime.new_session()  # 用户点“新对话”
        runtime.stop()         # 退出程序时调用

    参数：
        base_dir: 项目根目录（pi 的 cwd，也是相对路径基准）。
        on_event: 事件回调，见模块 docstring 的线程约定。

    环境变量（实例化时读取）：
        ``THESIS_PROVIDER`` / ``THESIS_MODEL`` 可覆盖 provider/model，无需改代码
        即可切换到 pi 内置模型目录中的更新版 Kimi 模型。
    """

    def __init__(
        self,
        base_dir: Path | str = BASE_DIR,
        on_event: Optional[Callable[[AgentEvent], None]] = None,
    ) -> None:
        self.base_dir: Path = Path(base_dir).resolve()
        # 环境变量在实例化时解析，便于测试用临时目录隔离（模块级常量仍保持契约）。
        self.agent_dir: Path = _resolve_agent_dir(self.base_dir)
        self.system_prompt_path: Path = _resolve_system_prompt(self.base_dir)
        # provider/model 同样支持 THESIS_PROVIDER / THESIS_MODEL 覆盖。
        self.provider: str = _resolve_provider()
        self.model: str = _resolve_model()
        self.on_event = on_event

        self._lock = threading.RLock()
        self._write_lock = threading.Lock()

        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=200)
        self._pending: dict[str, concurrent.futures.Future] = {}
        self._api_key: Optional[str] = None

        self._busy = False
        self._prompt_active = False
        self._starting = False
        self._stopping = False
        self._aborting = False
        self._aborted = False
        self._aux_bins_checked = False
        self._last_error: Optional[str] = None
        self._last_usage: dict = {}
        self._stream_parts: list[str] = []
        self._last_text = ""
        self._session_state: dict = {}
        self._task_started_at: Optional[float] = None
        self._turn_index = 0

    # ------------------------------------------------------------ 只读属性
    @property
    def busy(self) -> bool:
        """是否有任务正在处理（从 ``ask()`` 发出到 ``done`` 事件之间为 True）。"""
        with self._lock:
            return bool(self._busy)

    @property
    def last_assistant_text(self) -> str:
        """最近一次 assistant 回复的完整文本（流式过程中为已累积的部分）。"""
        with self._lock:
            return self._last_text or "".join(self._stream_parts)

    @property
    def api_key_configured(self) -> bool:
        """当前长驻进程是否已经带着密钥启动。"""
        with self._lock:
            return bool(self._api_key)

    # ------------------------------------------------------------ 环境检查
    def check_available(self) -> tuple[bool, str]:
        """检查 node + pi CLI 是否可用，并探测 pi 版本。

        返回 ``(是否可用, 中文说明)``；不会抛异常（环境问题通过返回值表达）。
        """
        try:
            node = find_node()
            pi_cli = find_pi_cli()
        except PiUnavailable as exc:
            return False, str(exc)
        try:
            version = detect_pi_version(
                node,
                pi_cli,
                cwd=self.base_dir,
                env=self._build_env(None),
            )
        except PiUnavailable as exc:
            return False, str(exc)

        message = f"pi {version} 可用（node: {node}）"
        warnings: list[str] = []
        prompt_path = self.system_prompt_path
        if not prompt_path.is_file():
            warnings.append(f"缺少系统提示词 {prompt_path}")
        ext_file = self._find_extension_file()
        if ext_file is None:
            warnings.append(f"缺少 pi 扩展 {self.agent_dir / 'extensions' / 'thesis-tools.ts'}")
        if warnings:
            message += "；注意：" + "；".join(warnings)
        return True, message

    # ------------------------------------------------------------ 生命周期
    def start(self, api_key: str) -> None:
        """启动长驻 RPC 进程并等待 ``get_state`` 成功。

        已经用同一密钥启动时直接返回；密钥变化会先 ``stop()`` 再重启。
        失败时抛 :class:`PiUnavailable`（缺 node/pi）或 :class:`PiRuntimeError`
        （缺密钥/扩展/系统提示词、进程启动即退出、握手超时等），消息为中文。
        """
        key = (api_key or "").strip()
        if not key:
            raise PiRuntimeError(
                "未提供 Kimi（Moonshot）API 密钥。\n"
                "请在 GUI 顶部“kimi API 密钥”输入框填写密钥后重试。"
            )

        with self._lock:
            existing = self._proc
            if existing is not None and existing.poll() is None and self._api_key == key:
                return

        node = find_node()  # PiUnavailable
        pi_cli = find_pi_cli()  # PiUnavailable
        prompt_path = self._require_system_prompt()
        self._require_extension_file()
        self._ensure_aux_tool_bins()

        if existing is not None:
            self.stop()

        argv = self._build_rpc_argv(node, pi_cli, prompt_path)
        env = self._build_env(key)
        logger.debug("启动 pi RPC: %s", argv)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(self.base_dir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=CREATE_NO_WINDOW,
            )
        except FileNotFoundError as exc:
            raise PiUnavailable(
                f"无法启动 pi 进程：node 或 cli.js 不存在。\n{exc}"
            ) from exc
        except OSError as exc:
            raise PiUnavailable(f"无法启动 pi 进程（{exc}）。请检查 node 与 pi 的安装。") from exc

        with self._lock:
            self._proc = proc
            self._api_key = key
            self._starting = True
            self._stopping = False
            self._stderr_tail.clear()
            self._pending.clear()
            self._session_state = {}
            self._last_text = ""
            self._stream_parts = []
            self._last_usage = {}
            self._last_error = None
            self._turn_index = 0

        reader = threading.Thread(
            target=self._reader_loop, args=(proc,), name="pi-rpc-reader", daemon=True
        )
        stderr_reader = threading.Thread(
            target=self._stderr_loop, args=(proc,), name="pi-rpc-stderr", daemon=True
        )
        with self._lock:
            self._reader_thread = reader
            self._stderr_thread = stderr_reader
        reader.start()
        stderr_reader.start()

        # 只有 get_state 成功响应才算就绪（RPC 模式启动后 stdout 是静默的）。
        try:
            response = self._request({"id": _new_request_id("state"), "type": "get_state"}, timeout=STARTUP_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - 统一转成可读中文错误
            stderr_tail = self._stderr_snapshot()
            with self._lock:
                self._starting = False
            self._cleanup_process(proc)
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                self._api_key = None
            raise PiRuntimeError(self._format_startup_error(exc, stderr_tail)) from exc

        data = response.get("data") if isinstance(response, dict) else {}
        data = data if isinstance(data, dict) else {}
        with self._lock:
            self._starting = False
            self._session_state = data

        model = data.get("model") or {}
        provider = model.get("provider") or self.provider
        model_id = model.get("id") or self.model
        self._emit(
            AgentEvent(
                "status",
                text=f"pi 运行时已就绪（{provider} · {model_id}）",
                data={"phase": "ready", "provider": provider, "model": model_id},
            )
        )
        self._emit(
            AgentEvent(
                "session",
                text="pi 会话已就绪",
                data={
                    "new_session": False,
                    "session_id": data.get("sessionId"),
                    "message_count": data.get("messageCount"),
                },
            )
        )

    def stop(self) -> None:
        """停止长驻进程（幂等；保证 3 秒内返回）。

        实现方式：若正在处理任务，先尽力写一条 ``abort``；随后直接
        ``terminate()`` / ``kill()`` 并关闭管道、join 线程。**不**通过关闭 stdin
        让 pi 自然退出（Windows 上可能触发 libuv 断言崩溃），也**不**信任退出码。
        """
        with self._lock:
            proc = self._proc
            was_active = self._prompt_active
            busy = self._busy
            last_usage = dict(self._last_usage or {})
            last_text = self._last_text or "".join(self._stream_parts)
            # 先“认领”任务收尾：读取线程若随后到达 agent_settled，不会再发第二个 done。
            claimed_done = bool(was_active)
            if claimed_done:
                self._prompt_active = False
                self._busy = False
                self._aborting = False
            self._stopping = True

        if proc is None:
            with self._lock:
                self._reset_after_stop()
            if claimed_done:
                self._emit(
                    AgentEvent("status", text="已停止当前任务。", data={"phase": "stopped"})
                )
                self._emit(
                    self._make_done_event(
                        ok=False,
                        error="操作已停止",
                        usage=last_usage,
                        text=last_text,
                        aborted=True,
                    )
                )
            return

        # 尽力通知 pi 取消当前生成（不等待响应，避免阻塞 UI）。
        if busy and proc.poll() is None:
            try:
                self._send_obj({"id": _new_request_id("abort"), "type": "abort"})
            except Exception:  # noqa: BLE001 - 进程可能已死，忽略
                pass

        try:
            self._cleanup_process(proc)
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(PiRuntimeError("pi 运行时已停止"))
                self._pending.clear()
                self._reset_after_stop()

        if claimed_done:
            # 让 GUI 的 busy 状态收尾（停止也算一次任务结束）。
            self._emit(
                AgentEvent("status", text="已停止当前任务。", data={"phase": "stopped"})
            )
            self._emit(
                self._make_done_event(
                    ok=False,
                    error="操作已停止",
                    usage=last_usage,
                    text=last_text,
                    aborted=True,
                )
            )

    def _reset_after_stop(self) -> None:
        """清理与已停止进程相关的状态（调用方需持有 ``self._lock``）。"""
        self._api_key = None
        self._busy = False
        self._prompt_active = False
        self._aborting = False
        self._aborted = False
        self._starting = False
        self._stopping = False
        self._task_started_at = None

    # ------------------------------------------------------------ 任务操作
    def ask(
        self,
        prompt: str,
        *,
        api_key: str,
        model: Optional[str] = None,
        provider: Optional[str] = None,
    ) -> None:
        """提交一条 prompt（非阻塞）。

        * 进程未启动 / 密钥变化 → 先 ``start()``；
        * 写入 ``{"type": "prompt", "message": ...}`` 后立即返回，模型输出通过
          ``on_event`` 的 ``text_delta`` / 工具事件流式到达，最终以 ``done`` 结束；
        * 正在处理任务时再调用会抛 :class:`PiRuntimeError`（中文）；
        * ``model`` / ``provider`` 可按任务覆盖（GUI 的「Agent 模型」下拉框）。
          与当前 pi 进程配置不同时会先 ``stop()`` 重启进程，**会话上下文会清空**；
          传 ``None`` 或空串表示沿用当前配置。

        prompt 走 stdin 的 JSON 行，长度不受 Windows argv 限制。
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise PiRuntimeError("任务内容为空，未发送。")
        key = (api_key or "").strip()
        if not key:
            raise PiRuntimeError(
                "未提供 Kimi（Moonshot）API 密钥。\n"
                "请在 GUI 顶部“kimi API 密钥”输入框填写密钥后重试。"
            )

        with self._lock:
            if self._prompt_active or self._busy:
                raise PiRuntimeError(
                    "pi 正在处理上一条任务，请等待完成或先点击“停止”。"
                )

        # 模型/provider 覆盖：变化时重启进程（RPC 的 --model 只能在启动时指定）。
        changed: list[str] = []
        model_override = (model or "").strip()
        provider_override = (provider or "").strip()
        if model_override and model_override != self.model:
            changed.append(model_override)
            self.model = model_override
        if provider_override and provider_override != self.provider:
            changed.append(provider_override)
            self.provider = provider_override
        if changed:
            with self._lock:
                have_proc = self._proc is not None and self._proc.poll() is None
            if have_proc:
                self.stop()
            self._emit(
                AgentEvent(
                    "status",
                    text=f"已切换运行配置（provider={self.provider} · model={self.model}），"
                    "将在下一次模型调用时生效。",
                    data={
                        "phase": "config_change",
                        "provider": self.provider,
                        "model": self.model,
                    },
                )
            )

        with self._lock:
            proc = self._proc
        needs_start = proc is None or proc.poll() is not None or self._api_key != key
        if needs_start:
            self._emit(
                AgentEvent("status", text="正在启动 pi 运行时…", data={"phase": "starting"})
            )
            if proc is not None:
                self.stop()
            try:
                self.start(key)
            except (PiUnavailable, PiRuntimeError) as exc:
                message = str(exc)
                self._emit(
                    AgentEvent("error", text=message, data={"phase": "startup"})
                )
                self._emit(
                    self._make_done_event(ok=False, error=message, usage={}, text="", aborted=False)
                )
                raise

        with self._lock:
            self._prompt_active = True
            self._busy = True
            self._aborting = False
            self._aborted = False
            self._last_error = None
            self._last_usage = {}
            self._last_text = ""
            self._stream_parts = []
            self._turn_index = 0
            self._task_started_at = time.monotonic()

        self._emit(
            AgentEvent("status", text="任务已提交，等待 pi 处理…", data={"phase": "prompt_sent"})
        )
        try:
            self._send_obj({"id": _new_request_id("prompt"), "type": "prompt", "message": prompt})
        except Exception as exc:  # noqa: BLE001 - 进程可能已死，转成可读错误
            message = f"无法向 pi 发送任务：{exc}"
            self._emit(AgentEvent("error", text=message, data={"phase": "send"}))
            events = self._finish_active(ok=False, error=message, aborted=False)
            for event in events:
                self._emit(event)
            raise PiRuntimeError(message) from exc

    def abort(self) -> None:
        """请求取消当前任务（非阻塞；永不阻塞 UI）。

        ``abort`` 命令的响应会在 pi 收尾（``agent_settled``）之后才返回，因此
        这里只写命令不等待；最终以 ``done``（``ok=False``，``aborted=True``）结束。
        """
        with self._lock:
            proc = self._proc
            active = self._prompt_active
            self._aborting = True
        if proc is None or proc.poll() is not None:
            return
        if not active:
            return
        try:
            self._send_obj({"id": _new_request_id("abort"), "type": "abort"})
        except Exception:  # noqa: BLE001 - 交给读取线程的 EOF 处理去报错
            return
        self._emit(
            AgentEvent("status", text="正在停止当前任务…", data={"phase": "aborting"})
        )

    def new_session(self) -> None:
        """开一个新会话（非阻塞）。进程未启动时只清空本地状态。"""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            with self._lock:
                self._last_text = ""
                self._stream_parts = []
                self._last_usage = {}
                self._last_error = None
                self._session_state = {}
            self._emit(
                AgentEvent(
                    "session",
                    text="已重置本地会话（pi 尚未启动，将在首条任务时启动）",
                    data={"new_session": True, "pending": True, "session_id": None},
                )
            )
            return
        try:
            self._send_obj({"id": _new_request_id("new"), "type": "new_session"})
        except Exception as exc:  # noqa: BLE001
            self._emit(
                AgentEvent("error", text=f"无法新建会话：{exc}", data={"phase": "new_session"})
            )
            return
        self._emit(
            AgentEvent("status", text="正在新建 pi 会话…", data={"phase": "new_session_sent"})
        )

    # ------------------------------------------------------------ 一次性 JSON 降级
    def run_once_json(
        self,
        prompt: str,
        *,
        api_key: str,
        timeout: float = 600,
        cancel_event: Optional[threading.Event] = None,
        system_prompt_path: Optional[Path] = None,
    ) -> str:
        """降级路径：用 ``pi --mode json`` 跑一次任务并返回最终 assistant 文本。

        * prompt 从 **stdin** 写入（pi 会把“无参数 prompt + 管道 stdin”合并成
          prompt），绝不放 argv；
        * 超时/取消时终止子进程并抛 :class:`PiRuntimeError`（中文）；
        * 模型返回错误（``stopReason == "error"``）或无任何文本时抛
          :class:`PiRuntimeError`，由调用方决定是否回退到 legacy 脚本。
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise PiRuntimeError("任务内容为空，未发送。")
        key = (api_key or "").strip()
        if not key:
            raise PiRuntimeError(
                "未提供 Kimi（Moonshot）API 密钥。\n"
                "请在 GUI 顶部“kimi API 密钥”输入框填写密钥后重试。"
            )
        node = find_node()
        pi_cli = find_pi_cli()
        prompt_file = (
            Path(system_prompt_path) if system_prompt_path is not None else self.system_prompt_path
        )
        if not prompt_file.is_file():
            raise PiRuntimeError(
                f"未找到系统提示词文件：{prompt_file}\n"
                "请确认项目 .pi/prompts/thesis_system.md 存在，或通过 THESIS_SYSTEM_PROMPT 指定。"
            )
        if timeout is None or timeout <= 0:
            timeout = 600.0

        argv = _launch_prefix(node, pi_cli) + self._build_common_flags(
            prompt_file, mode="json"
        )
        env = self._build_env(key)
        logger.debug("启动 pi JSON one-shot: %s", argv)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(self.base_dir),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            raise PiUnavailable(f"无法启动 pi（一次性 JSON 模式）：{exc}") from exc

        lines: queue.Queue[Optional[bytes]] = queue.Queue()
        stderr_lines: collections.deque[str] = collections.deque(maxlen=200)

        def _read_stdout() -> None:
            try:
                assert proc.stdout is not None
                while True:
                    raw = proc.stdout.readline()
                    if not raw:
                        break
                    lines.put(raw)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)

        def _read_stderr() -> None:
            try:
                assert proc.stderr is not None
                while True:
                    raw = proc.stderr.readline()
                    if not raw:
                        break
                    stderr_lines.append(_decode(raw).rstrip("\r\n"))
            except (OSError, ValueError):
                pass

        def _write_stdin() -> None:
            try:
                assert proc.stdin is not None
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.flush()
                proc.stdin.close()
            except (OSError, ValueError):
                pass

        for target, name in (
            (_read_stdout, "pi-json-stdout"),
            (_read_stderr, "pi-json-stderr"),
            (_write_stdin, "pi-json-stdin"),
        ):
            threading.Thread(target=target, name=name, daemon=True).start()

        deadline = time.monotonic() + float(timeout)
        accumulated: list[str] = []
        last_text = ""
        llm_error = ""
        stop_reason = ""
        finish_reason: Optional[str] = None

        def _abort_process(reason: str) -> None:
            self._cleanup_process(proc)

        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    finish_reason = "操作已取消"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    finish_reason = f"pi 一次性调用超时（>{timeout:.0f}s）"
                    break
                try:
                    raw = lines.get(timeout=min(0.2, max(remaining, 0.01)))
                except queue.Empty:
                    continue
                if raw is None:
                    break
                line = _decode(raw)
                if line.endswith("\r"):
                    line = line[:-1]
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("JSON 模式忽略非 JSON 行：%r", line[:200])
                    continue
                if not isinstance(event, dict):
                    continue
                etype = event.get("type")
                if etype == "message_update":
                    ame = event.get("assistantMessageEvent") or {}
                    if isinstance(ame, dict) and ame.get("type") == "text_delta":
                        delta = ame.get("delta")
                        if isinstance(delta, str) and delta:
                            accumulated.append(delta)
                elif etype == "message_end":
                    message = event.get("message") or {}
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        text = _extract_text_from_message(message)
                        if text:
                            last_text = text
                        reason = message.get("stopReason")
                        if isinstance(reason, str):
                            stop_reason = reason
                        if reason == "error":
                            llm_error = humanize_pi_error(
                                message.get("errorMessage") or "模型调用失败"
                            )
                        usage = message.get("usage")
                        if isinstance(usage, dict) and usage:
                            self._last_usage = usage
                elif etype == "agent_end":
                    text = _extract_last_assistant_text(event.get("messages"))
                    if text:
                        last_text = text
        finally:
            if finish_reason is not None:
                _abort_process(finish_reason)
            else:
                # 读取线程 EOF 后进程通常已退出；给它一点时间。
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _abort_process("进程未退出")
            # 兜底清理（幂等）。
            self._cleanup_process(proc)

        if finish_reason is not None:
            raise PiRuntimeError(finish_reason + "。")
        stderr_text = "\n".join(stderr_lines).strip()
        final_text = last_text or "".join(accumulated)
        if final_text.strip():
            return final_text
        if llm_error:
            raise PiRuntimeError(llm_error)
        if stop_reason == "aborted":
            raise PiRuntimeError("操作已取消。")
        returncode = proc.returncode
        if returncode not in (0, None):
            detail = stderr_text or f"退出码 {returncode}"
            raise PiRuntimeError(
                f"pi 一次性调用失败（退出码 {returncode}）：\n{_summarize(detail, 1500)}"
            )
        if stderr_text:
            raise PiRuntimeError(
                "pi 没有返回任何文本。\npi 错误输出：\n" + _summarize(stderr_text, 1500)
            )
        raise PiRuntimeError(
            "pi 没有返回任何文本（可能没有产生回复）。请检查密钥、模型与系统提示词配置。"
        )

    # ------------------------------------------------------------ 命令/进程内部实现
    def _build_common_flags(self, prompt_file: Path, *, mode: str) -> list[str]:
        """构造 RPC/JSON 两种模式共用的 pi 命令行参数（不含 node/cli 前缀）。

        provider/model 使用 ``self.provider`` / ``self.model``（支持
        ``THESIS_PROVIDER`` / ``THESIS_MODEL`` 环境变量覆盖）。
        """
        return [
            "--mode",
            mode,
            "--provider",
            self.provider,
            "--model",
            self.model,
            "--no-session",
            "--offline",
            "--tools",
            AGENT_TOOLS,
            "--system-prompt",
            str(prompt_file),
        ]

    def _build_rpc_argv(self, node: Path, pi_cli: Path, prompt_file: Path) -> list[str]:
        return _launch_prefix(node, pi_cli) + self._build_common_flags(prompt_file, mode="rpc")

    def _build_env(self, api_key: Optional[str]) -> dict[str, str]:
        """构造子进程环境变量。

        * ``PI_CODING_AGENT_DIR``：隔离到项目 ``.pi/agent``（扩展从这里自动加载，
          无需 ``--approve``；也不会污染用户 ``~/.pi``）；
        * ``PI_OFFLINE=1``：禁止启动期联网（更新检查/遥测），不影响 LLM 调用；
        * ``MOONSHOT_API_KEY``：密钥只走 env，绝不进 argv / 日志；
        * ``THESIS_PYTHON``：扩展内调用 Python 助手时使用的解释器；
        * ``PYTHONIOENCODING=utf-8``：避免子进程中文输出 mojibake；
        * ``NO_PROXY`` 追加 127.0.0.1/localhost：离线 mock 测试与本地服务不被代理。
        """
        env = dict(os.environ)
        env.update(
            {
                "PI_CODING_AGENT_DIR": str(self.agent_dir),
                "PI_OFFLINE": "1",
                "THESIS_PYTHON": sys.executable,
                "PYTHONIOENCODING": "utf-8",
            }
        )
        if api_key:
            env["MOONSHOT_API_KEY"] = api_key
        else:
            env.pop("MOONSHOT_API_KEY", None)
        no_proxy = env.get("NO_PROXY") or env.get("no_proxy") or ""
        no_proxy_items = [item for item in no_proxy.split(",") if item.strip()]
        for local in ("127.0.0.1", "localhost"):
            if local not in no_proxy_items:
                no_proxy_items.append(local)
        env["NO_PROXY"] = ",".join(no_proxy_items)
        return env

    def _ensure_aux_tool_bins(self) -> None:
        """确保 pi 的 grep/find 依赖（rg/fd）可用。

        pi 会优先使用 ``<PI_CODING_AGENT_DIR>/bin`` 下的 rg/fd，其次 PATH，最后才
        尝试联网下载（本项目的 ``--offline`` 禁止下载）。首次启动时若本机全局
        pi 配置（``~/.pi/agent/bin``）里已有这两个二进制，直接复制到项目配置目录，
        避免离线环境 grep/find 不可用（实测否则会报 "ripgrep (rg) is not available"）。
        """
        if self._aux_bins_checked:
            return
        self._aux_bins_checked = True
        exe = ".exe" if os.name == "nt" else ""
        bin_dir = Path(self.agent_dir) / "bin"
        try:
            bin_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        global_bin = Path.home() / ".pi" / "agent" / "bin"
        prepared: list[str] = []
        for name in ("rg", "fd"):
            dest = bin_dir / f"{name}{exe}"
            if dest.is_file() or shutil.which(name):
                continue
            source = global_bin / f"{name}{exe}"
            if source.is_file():
                try:
                    shutil.copy2(source, dest)
                    prepared.append(name)
                except OSError:
                    pass
        if prepared:
            self._emit(
                AgentEvent(
                    "status",
                    text="已准备 grep/find 依赖：" + "、".join(prepared),
                    data={"phase": "aux_tools", "tools": prepared},
                )
            )

    def _find_extension_file(self) -> Optional[Path]:
        """查找项目扩展文件 ``<agent_dir>/extensions/thesis-tools.ts``（兼容 .js）。"""
        extensions_dir = self.agent_dir / "extensions"
        for name in ("thesis-tools.ts", "thesis-tools.js"):
            candidate = extensions_dir / name
            if candidate.is_file():
                return candidate
        return None

    def _require_extension_file(self) -> Path:
        extension = self._find_extension_file()
        if extension is None:
            expected = self.agent_dir / "extensions" / "thesis-tools.ts"
            raise PiRuntimeError(
                f"未找到 pi 扩展文件：{expected}\n"
                "请确认项目 .pi/agent/extensions/thesis-tools.ts 存在（它提供 thesis_* 工具，"
                "负责 PDF 解析、大纲校验/保存与润色结果落盘）。"
            )
        return extension

    def _require_system_prompt(self) -> Path:
        prompt_file = Path(self.system_prompt_path)
        if not prompt_file.is_file():
            raise PiRuntimeError(
                f"未找到系统提示词文件：{prompt_file}\n"
                "请确认项目 .pi/prompts/thesis_system.md 存在，"
                "或通过环境变量 THESIS_SYSTEM_PROMPT 指定。"
            )
        return prompt_file

    # ------------------------------------------------------------ 协议读写
    def _send_obj(self, obj: dict) -> None:
        """把一条命令写成 UTF-8 JSON 行并 flush（线程安全）。"""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise OSError("pi 进程未运行")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise OSError(f"写入 pi stdin 失败：{exc}") from exc

    def _request(self, command: dict, *, timeout: float) -> dict:
        """发送带 ``id`` 的命令并等待同 ``id`` 的 ``response``。"""
        request_id = str(command.get("id") or _new_request_id("req"))
        command = dict(command)
        command["id"] = request_id
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._pending[request_id] = future
        try:
            self._send_obj(command)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._pending.pop(request_id, None)
            raise PiRuntimeError(f"无法向 pi 发送命令 {command.get('type')}：{exc}") from exc
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            raise PiRuntimeError(
                f"等待 pi 响应超时（{timeout:.0f}s，命令 {command.get('type')}）。"
            ) from exc

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        """stdout 二进制行读取线程：按 b'\\n' 切行、去尾部 CR、UTF-8 解码后分发。"""
        stdout = proc.stdout
        if stdout is None:
            self._on_reader_end(proc)
            return
        try:
            while True:
                try:
                    raw = stdout.readline()
                except (OSError, ValueError):
                    break
                if not raw:
                    break
                line = _decode(raw)
                if line.endswith("\r"):
                    line = line[:-1]
                if not line.strip():
                    continue
                for event in self._handle_line(line):
                    self._emit(event)
        finally:
            self._on_reader_end(proc)

    def _stderr_loop(self, proc: subprocess.Popen) -> None:
        """stderr 收集线程：只保留尾部若干行，供启动失败/进程退出时诊断。"""
        stderr = proc.stderr
        if stderr is None:
            return
        try:
            while True:
                try:
                    raw = stderr.readline()
                except (OSError, ValueError):
                    break
                if not raw:
                    break
                line = _decode(raw).rstrip("\r\n")
                if line:
                    self._stderr_tail.append(line)
                    logger.debug("pi stderr: %s", line)
        finally:
            pass

    def _stderr_snapshot(self, limit: int = 20) -> str:
        lines = list(self._stderr_tail)[-limit:]
        return "\n".join(lines).strip()

    def _emit(self, event: AgentEvent) -> None:
        """安全地触发 ``on_event`` 回调（回调异常只记录日志，不影响协议读取）。

        注意：本方法可能在读取线程被调用；宿主必须保证回调线程安全
        （GUI 里只应 ``queue.put``）。
        """
        callback = self.on_event
        if callback is None:
            return
        try:
            callback(event)
        except Exception:  # noqa: BLE001 - 用户回调异常不能拖垮运行时
            logger.exception("on_event 回调抛出异常（已忽略）：%r", event.kind)

    def _on_reader_end(self, proc: subprocess.Popen) -> None:
        """stdout EOF：进程退出。启动阶段由 ``start()`` 处理；运行期报错并收尾。"""
        with self._lock:
            if self._stopping:
                return
            if self._proc is not proc:
                return
            if self._starting:
                # 启动阶段进程退出：唤醒正在等待 get_state 的 future。
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(
                            PiRuntimeError("pi 进程在启动阶段退出（可能缺少依赖或配置错误）")
                        )
                self._pending.clear()
                return
            active = self._prompt_active
            stderr_tail = self._stderr_snapshot()
            message = "pi 进程意外退出。"
            if stderr_tail:
                message += "\npi 错误输出：\n" + _summarize(stderr_tail, 1500)
            self._last_error = message

        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(PiRuntimeError(message))
        with self._lock:
            self._pending.clear()

        self._emit(AgentEvent("error", text=message, data={"phase": "process"}))
        if active:
            for event in self._finish_active(ok=False, error=message, aborted=False):
                self._emit(event)
        else:
            self._emit(
                AgentEvent("status", text="pi 运行时进程已退出。", data={"phase": "exited"})
            )

    def _handle_line(self, line: str) -> list[AgentEvent]:
        """解析并分发一行 stdout（``response`` 或事件）。"""
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("RPC 忽略非 JSON 行：%r", line[:300])
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get("type") == "response":
            return self._handle_response(payload)
        return self._handle_event(payload)

    def _handle_response(self, payload: dict) -> list[AgentEvent]:
        """处理命令响应：优先按 ``id`` 唤醒等待的 future，其次做异步命令收尾。"""
        request_id = payload.get("id")
        future: Optional[concurrent.futures.Future] = None
        if request_id is not None:
            with self._lock:
                future = self._pending.pop(str(request_id), None)
        if future is not None:
            if not future.done():
                if payload.get("success"):
                    future.set_result(payload)
                else:
                    command = payload.get("command") or "command"
                    raw_error = payload.get("error") or "未知错误"
                    future.set_exception(
                        PiRuntimeError(
                            f"pi 命令 {command} 执行失败：{humanize_pi_error(raw_error)}"
                        )
                    )
            return []

        command = payload.get("command") or ""
        success = bool(payload.get("success"))
        raw_error = payload.get("error") or ""

        if command == "prompt" and not success:
            message = humanize_pi_error(raw_error)
            self._emit(AgentEvent("error", text=message, data={"phase": "prompt_rejected"}))
            return self._finish_active(ok=False, error=message, aborted=False)

        if command == "new_session":
            if success:
                data = payload.get("data") or {}
                cancelled = bool(data.get("cancelled")) if isinstance(data, dict) else False
                with self._lock:
                    self._last_text = ""
                    self._stream_parts = []
                    self._last_usage = {}
                    self._last_error = None
                if cancelled:
                    return [
                        AgentEvent(
                            "status",
                            text="新建会话被扩展取消。",
                            data={"phase": "new_session", "cancelled": True},
                        )
                    ]
                return [
                    AgentEvent(
                        "session",
                        text="已开始新的 pi 会话。",
                        data={
                            "new_session": True,
                            "cancelled": False,
                            "session_id": None,
                            "pending": False,
                        },
                    )
                ]
            return [
                AgentEvent(
                    "error",
                    text="新建会话失败：" + humanize_pi_error(raw_error),
                    data={"phase": "new_session"},
                )
            ]

        if command == "abort":
            if success:
                return [
                    AgentEvent(
                        "status", text="pi 已确认停止请求。", data={"phase": "abort_ack"}
                    )
                ]
            return [
                AgentEvent(
                    "status",
                    text="停止请求未成功：" + humanize_pi_error(raw_error),
                    data={"phase": "abort_failed"},
                )
            ]

        if command == "parse":
            return [
                AgentEvent(
                    "error",
                    text="pi 无法解析客户端命令（协议错误）。",
                    data={"phase": "protocol", "raw": _summarize(raw_error, 500)},
                )
            ]

        if not success and command:
            return [
                AgentEvent(
                    "error",
                    text=f"pi 命令 {command} 执行失败：{humanize_pi_error(raw_error)}",
                    data={"phase": "command", "command": command},
                )
            ]
        return []

    def _handle_event(self, payload: dict) -> list[AgentEvent]:
        """把 pi 的流事件映射为 :class:`AgentEvent`。"""
        event_type = payload.get("type")
        if event_type == "agent_start":
            return [AgentEvent("status", text="pi 开始处理任务…", data={"phase": "agent_start"})]

        if event_type == "turn_start":
            with self._lock:
                self._turn_index += 1
                turn = self._turn_index
            return [AgentEvent("status", text=f"第 {turn} 轮生成开始…", data={"phase": "turn_start", "turn": turn})]

        if event_type == "message_start":
            message = payload.get("message") or {}
            if isinstance(message, dict) and message.get("role") == "assistant":
                with self._lock:
                    self._stream_parts = []
            return []

        if event_type == "message_update":
            return self._handle_message_update(payload)

        if event_type == "message_end":
            return self._handle_message_end(payload)

        if event_type == "tool_execution_start":
            tool_name = payload.get("toolName") or "未知工具"
            args = payload.get("args") or {}
            return [
                AgentEvent(
                    "tool_start",
                    text=f"正在调用工具：{tool_name}",
                    data={
                        "toolCallId": payload.get("toolCallId"),
                        "toolName": tool_name,
                        "args": args,
                    },
                )
            ]

        if event_type == "tool_execution_update":
            tool_name = payload.get("toolName") or "未知工具"
            partial = payload.get("partialResult") or {}
            partial_text = _summarize(_tool_result_text(partial))
            details = partial.get("details") if isinstance(partial, dict) else None
            text = partial_text.strip() or f"工具 {tool_name} 运行中…"
            return [
                AgentEvent(
                    "tool_update",
                    text=text,
                    data={
                        "toolCallId": payload.get("toolCallId"),
                        "toolName": tool_name,
                        "args": payload.get("args") or {},
                        "text": partial_text,
                        "details": details,
                    },
                )
            ]

        if event_type == "tool_execution_end":
            tool_name = payload.get("toolName") or "未知工具"
            is_error = bool(payload.get("isError"))
            result = payload.get("result") or {}
            result_text = _summarize(_tool_result_text(result))
            details = result.get("details") if isinstance(result, dict) else None
            if is_error:
                summary = f"工具 {tool_name} 执行失败：" + (
                    result_text.strip()[:300] or "未知错误"
                )
            else:
                summary = f"工具 {tool_name} 执行完成。"
            return [
                AgentEvent(
                    "tool_end",
                    text=summary,
                    data={
                        "toolCallId": payload.get("toolCallId"),
                        "toolName": tool_name,
                        "is_error": is_error,
                        "result_text": result_text,
                        "details": details,
                    },
                )
            ]

        if event_type == "agent_end":
            messages = payload.get("messages")
            text = _extract_last_assistant_text(messages)
            with self._lock:
                if text:
                    self._last_text = text
                if isinstance(messages, list):
                    for message in reversed(messages):
                        if not isinstance(message, dict) or message.get("role") != "assistant":
                            continue
                        usage = message.get("usage")
                        if isinstance(usage, dict) and usage:
                            self._last_usage = usage
                        stop_reason = message.get("stopReason")
                        if stop_reason == "error":
                            self._last_error = humanize_pi_error(
                                message.get("errorMessage") or "模型调用失败"
                            )
                        elif stop_reason == "aborted":
                            self._aborted = True
                        else:
                            # 最后一次 assistant 消息正常结束：清掉此前重试尝试的错误。
                            self._last_error = None
                        break
                will_retry = bool(payload.get("willRetry"))
            if will_retry:
                return [
                    AgentEvent(
                        "status", text="模型调用出错，pi 将自动重试…", data={"phase": "retry"}
                    )
                ]
            return []

        if event_type == "agent_settled":
            return self._handle_agent_settled()

        if event_type == "auto_retry_start":
            attempt = payload.get("attempt")
            max_attempts = payload.get("maxAttempts")
            detail = humanize_pi_error(payload.get("errorMessage") or "")
            return [
                AgentEvent(
                    "status",
                    text=f"模型请求出错，正在自动重试（{attempt}/{max_attempts}）：{detail}",
                    data={"phase": "auto_retry", "attempt": attempt, "maxAttempts": max_attempts},
                )
            ]

        if event_type == "auto_retry_end":
            if payload.get("success"):
                with self._lock:
                    # 重试成功：清除失败尝试留下的错误，避免 agent_settled 误报失败。
                    self._last_error = None
                return [
                    AgentEvent("status", text="自动重试成功。", data={"phase": "auto_retry_ok"})
                ]
            error = humanize_pi_error(payload.get("finalError") or "自动重试最终失败")
            with self._lock:
                self._last_error = error
            return [
                AgentEvent(
                    "status",
                    text="自动重试最终失败：" + error,
                    data={"phase": "auto_retry_failed"},
                )
            ]

        if event_type == "compaction_start":
            return [
                AgentEvent("status", text="上下文压缩中…", data={"phase": "compaction_start"})
            ]

        if event_type == "compaction_end":
            if payload.get("errorMessage"):
                return [
                    AgentEvent(
                        "status",
                        text="上下文压缩失败：" + humanize_pi_error(payload.get("errorMessage")),
                        data={"phase": "compaction_error"},
                    )
                ]
            return [AgentEvent("status", text="上下文压缩完成。", data={"phase": "compaction_end"})]

        if event_type == "extension_error":
            return [
                AgentEvent(
                    "error",
                    text="pi 扩展运行出错："
                    + _summarize(str(payload.get("error") or "未知错误"), 500),
                    data={"phase": "extension_error", "extensionPath": payload.get("extensionPath")},
                )
            ]

        if event_type == "extension_ui_request":
            return self._handle_extension_ui(payload)

        if event_type == "queue_update":
            return []

        if event_type == "bash_execution_update":
            return []

        logger.debug("RPC 忽略未知事件类型：%s", event_type)
        return []

    def _handle_message_update(self, payload: dict) -> list[AgentEvent]:
        ame = payload.get("assistantMessageEvent") or {}
        if not isinstance(ame, dict):
            return []
        update_type = ame.get("type")
        if update_type == "text_delta":
            delta = ame.get("delta")
            if not isinstance(delta, str) or not delta:
                return []
            with self._lock:
                self._stream_parts.append(delta)
            return [
                AgentEvent(
                    "text_delta",
                    text=delta,
                    data={"contentIndex": ame.get("contentIndex") or 0},
                )
            ]
        if update_type == "toolcall_end":
            tool_call = ame.get("toolCall") or {}
            name = tool_call.get("name") if isinstance(tool_call, dict) else None
            if name:
                return [
                    AgentEvent(
                        "status",
                        text=f"准备调用工具：{name}",
                        data={"phase": "toolcall", "toolName": name},
                    )
                ]
        return []

    def _handle_message_end(self, payload: dict) -> list[AgentEvent]:
        message = payload.get("message") or {}
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return []
        events: list[AgentEvent] = []
        final_text = _extract_text_from_message(message)
        stop_reason = message.get("stopReason")
        error_message = message.get("errorMessage")
        with self._lock:
            if final_text:
                self._last_text = final_text
            elif stop_reason in ("error", "aborted") and self._stream_parts:
                self._last_text = "".join(self._stream_parts)
            usage = message.get("usage")
            if isinstance(usage, dict) and usage:
                self._last_usage = usage
            if stop_reason == "error":
                self._last_error = humanize_pi_error(error_message or "模型调用失败")
            elif stop_reason == "aborted":
                self._aborted = True
            else:
                # 正常/工具调用轮次：此前尝试的错误已被重试成功覆盖，必须清掉，
                # 否则 agent_settled 会把「重试成功」的任务误报为失败。
                self._last_error = None
        if stop_reason == "error":
            events.append(
                AgentEvent(
                    "status",
                    text="模型调用出错：" + humanize_pi_error(error_message or "未知错误"),
                    data={"phase": "model_error", "raw_error": _summarize(str(error_message or ""), 800)},
                )
            )
        elif stop_reason == "aborted":
            events.append(
                AgentEvent("status", text="模型生成已中止。", data={"phase": "model_aborted"})
            )
        return events

    def _handle_agent_settled(self) -> list[AgentEvent]:
        """``agent_settled``：一次任务彻底结束，发 ``done`` 事件（ok/error/usage/text）。"""
        with self._lock:
            if not self._prompt_active:
                return []
            ok = not self._last_error and not self._aborted
            if self._aborted:
                error: Optional[str] = "操作已取消"
            elif self._last_error:
                error = self._last_error
            else:
                error = None
        events: list[AgentEvent] = []
        if ok:
            events.append(AgentEvent("status", text="任务完成。", data={"phase": "done", "ok": True}))
        elif self._aborted:
            events.append(AgentEvent("status", text="任务已取消。", data={"phase": "aborted"}))
        else:
            events.append(
                AgentEvent("error", text=error or "任务失败", data={"phase": "settled"})
            )
        events.extend(self._finish_active(ok=ok, error=error, aborted=self._aborted))
        return events

    def _handle_extension_ui(self, payload: dict) -> list[AgentEvent]:
        """RPC 扩展 UI 子协议：项目扩展不应弹窗；这里做防御性自动应答，避免挂住工具。"""
        method = payload.get("method")
        request_id = payload.get("id")
        if method in ("confirm", "select", "input", "editor"):
            response: dict[str, Any] = {"type": "extension_ui_response", "id": request_id}
            if method == "confirm":
                response["confirmed"] = True
            elif method == "select":
                options = payload.get("options") or []
                response["value"] = options[0] if options else None
            else:
                response["cancelled"] = True
            try:
                self._send_obj(response)
            except Exception:  # noqa: BLE001 - 进程可能已死，忽略
                pass
            return [
                AgentEvent(
                    "status",
                    text=f"已自动应答 pi 扩展对话框（{method}）。",
                    data={"phase": "extension_ui", "method": method, "id": request_id},
                )
            ]
        if method == "notify":
            message = payload.get("message") or "pi 通知"
            return [
                AgentEvent(
                    "status",
                    text=str(message),
                    data={"phase": "extension_notify", "notifyType": payload.get("notifyType")},
                )
            ]
        return []

    # ------------------------------------------------------------ 任务收尾辅助
    def _make_done_event(
        self,
        *,
        ok: bool,
        error: Optional[str],
        usage: Optional[dict],
        text: str,
        aborted: bool,
    ) -> AgentEvent:
        data = {
            "ok": bool(ok),
            "error": error,
            "usage": dict(usage or {}),
            "text": text or "",
            "aborted": bool(aborted),
        }
        if ok:
            summary = "任务完成"
        elif aborted:
            summary = "任务已取消"
        else:
            summary = "任务失败：" + (error or "未知错误")
        return AgentEvent("done", text=summary, data=data)

    def _finish_active(
        self, *, ok: bool, error: Optional[str], aborted: bool
    ) -> list[AgentEvent]:
        """结束当前任务：清 busy 状态并返回 ``done`` 事件（仅当有活动任务）。"""
        with self._lock:
            if not self._prompt_active:
                return []
            self._prompt_active = False
            self._busy = False
            self._aborting = False
            usage = dict(self._last_usage or {})
            text = self._last_text or "".join(self._stream_parts)
            self._task_started_at = None
        event = self._make_done_event(
            ok=ok, error=error, usage=usage, text=text, aborted=aborted
        )
        return [event]

    def _cleanup_process(self, proc: subprocess.Popen) -> None:
        """终止进程并回收管道/线程（不信任退出码，幂等、尽量不抛异常）。

        先 terminate/kill，**最后**才关闭 stdin：pi 在 Windows 上「关闭 stdin 后
        继续运行」可能触发 libuv 断言崩溃，我们完全不走“关闭 stdin 等自然退出”
        的路径，因此也不依赖退出码。
        """
        if proc.poll() is None:
            try:
                proc.terminate()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=_STOP_WAIT)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except (OSError, ValueError):
                    pass
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        current = threading.current_thread()
        with self._lock:
            threads = [self._reader_thread, self._stderr_thread]
        for thread in threads:
            if thread is not None and thread is not current and thread.is_alive():
                thread.join(timeout=1.0)

    def _format_startup_error(self, exc: BaseException, stderr_tail: str) -> str:
        """把启动异常 + pi stderr 组装成中文可操作错误。"""
        raw = str(exc).strip() or exc.__class__.__name__
        detail = stderr_tail.strip()
        hint = self._startup_hint(detail or raw)
        parts = [f"pi 启动失败：{raw}"]
        if detail and detail not in raw:
            parts.append("pi 错误输出：\n" + _summarize(detail, 1500))
        if hint:
            parts.append("建议：" + hint)
        return "\n".join(parts)

    @staticmethod
    def _startup_hint(text: str) -> str:
        low = text.lower()
        if "unknown provider" in low:
            return (
                "当前安装的 pi 可能不是 0.85.x（本项目依赖内置 moonshotai-cn provider）。"
                "请升级：npm i -g @earendil-works/pi-coding-agent。"
            )
        if "no api key" in low:
            return "请检查 GUI 中填写的 Kimi（Moonshot）API 密钥是否正确。"
        if "cannot find module" in low or "module_not_found" in low or "err_module" in low:
            return "pi 安装不完整，请重新安装：npm i -g @earendil-works/pi-coding-agent。"
        if "assertion failed" in low or "uv_handle" in low:
            return "pi 在 Windows 上异常退出（上游 libuv 缺陷），请重试或重启程序。"
        if "eperm" in low or "eacces" in low or "access is denied" in low:
            return "pi 配置目录不可写，请检查项目 .pi/agent 目录权限。"
        return (
            "请确认 Node.js（node.exe）可用、pi 版本为 0.85.x，且 .pi/agent 配置完整"
            "（可先运行 pi --version 自检）。"
        )


__all__ = [
    "BASE_DIR",
    "AGENT_DIR",
    "SYSTEM_PROMPT_PATH",
    "DEFAULT_PROVIDER",
    "DEFAULT_MODEL",
    "PROVIDER",
    "MODEL",
    "AGENT_TOOLS",
    "AgentEvent",
    "PiUnavailable",
    "PiRuntimeError",
    "PiRuntime",
    "build_outline_prompt",
    "build_polish_prompt",
    "build_chat_prompt",
    "find_node",
    "find_pi_cli",
    "detect_pi_version",
    "humanize_pi_error",
    "fetch_moonshot_models",
    "fetch_moonshot_balance",
]
