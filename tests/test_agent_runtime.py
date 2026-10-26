# -*- coding: utf-8 -*-
"""agent_runtime.py 的离线测试（pytest）。

测试分三类：

1. 纯单元测试：常量/数据类、prompt 构造函数、CLI 发现、事件解析映射、
   错误文案、启动前校验、``run_once_json`` 的 argv/stdin/超时/取消路径
   （用“假 CLI”脚本代替真实 pi，完全离线、毫秒级）。
2. 事件解析 fixture 测试：``tests/fixtures/*.jsonl`` 是按 pi 0.85.1 实测事件
   （事件语义见 ``docs/pi-agent-integration.md`` §5）
   整理的 JSONL，喂给 ``PiRuntime._handle_line`` 验证映射结果。
3. live 子进程测试：真实 ``node + pi --mode rpc`` 对本机 mock OpenAI 服务
   （``tests/mock_openai_server.py``）跑完整链路，验证流式 delta、工具事件、
   done/busy、abort、``stop() < 3s`` 与 ``run_once_json``。缺少 node/pi/mock
   时自动 skip。

注意：本沙箱里 pytest 的 ``tmp_path``（0o700 目录）之后可能不可写/不可列，
因此测试统一使用本文件自带的 ``_workdir()`` 创建普通权限目录，并在 teardown
尽力清理（可用 ``THESIS_TEST_TMP`` 指定其他临时根目录）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent_runtime as ar  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MOCK_SCRIPT = Path(__file__).resolve().parent / "mock_openai_server.py"


# --------------------------------------------------------------------------
# 通用工具：沙箱安全的临时目录 / 环境隔离 / 事件收集
# --------------------------------------------------------------------------


def _workdir(name: str) -> Path:
    """创建测试工作目录：不使用 0o700 的 tempfile（本沙箱下会不可写）。"""
    base = Path(os.environ.get("THESIS_TEST_TMP") or (ROOT / ".thesis" / "pytest-work"))
    base.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", name)[:40] or "case"
    directory = base / f"{safe}-{uuid.uuid4().hex[:10]}"
    directory.mkdir()
    return directory


def _rmtree_best_effort(path: Path, attempts: int = 6) -> None:
    """清理测试目录：Windows 上子进程句柄可能刚释放，短暂重试后放弃。"""
    for _ in range(attempts):
        if not path.exists():
            return
        try:
            shutil.rmtree(path)
            return
        except OSError:
            time.sleep(0.15)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def workdir(request):
    directory = _workdir(request.node.name)
    yield directory
    _rmtree_best_effort(directory)


def _read_fixture(name: str) -> list[str]:
    text = (FIXTURES / name).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def _mark_task_started(runtime: ar.PiRuntime) -> None:
    """单元测试里模拟 ``ask()`` 已提交任务后的内部状态。"""
    runtime._prompt_active = True
    runtime._busy = True
    runtime._aborted = False
    runtime._aborting = False
    runtime._last_error = None
    runtime._last_usage = {}
    runtime._last_text = ""
    runtime._stream_parts = []


def _feed(runtime: ar.PiRuntime, fixture_name: str) -> list[ar.AgentEvent]:
    """按读取线程的方式逐行分发 fixture，返回收集到的事件。"""
    collected: list[ar.AgentEvent] = []
    runtime.on_event = collected.append
    for line in _read_fixture(fixture_name):
        for event in runtime._handle_line(line):
            runtime._emit(event)
    return collected


def _simple_runtime(workdir: Path) -> ar.PiRuntime:
    project = workdir / "project"
    project.mkdir()
    return ar.PiRuntime(base_dir=project)


def _isolated_runtime(
    workdir: Path,
    monkeypatch,
    *,
    on_event=None,
    prompt_text: str = "你是 ThesisPolish 的论文润色助手。",
    with_extension: bool = True,
):
    """构造使用临时 PI_CODING_AGENT_DIR / 系统提示词的运行时；返回 (runtime, paths)。"""
    project = workdir / "project"
    project.mkdir()
    agent = workdir / "agent"
    if with_extension:
        extensions = agent / "extensions"
        extensions.mkdir(parents=True, exist_ok=True)
        shutil.copy2(FIXTURES / "minimal_thesis_tools.ts", extensions / "thesis-tools.ts")
    prompt_file = workdir / "thesis_system.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")
    monkeypatch.setenv("THESIS_PI_AGENT_DIR", str(agent))
    monkeypatch.setenv("THESIS_SYSTEM_PROMPT", str(prompt_file))
    runtime = ar.PiRuntime(base_dir=project, on_event=on_event)
    return runtime, {"project": project, "agent": agent, "prompt": prompt_file}


def _write_fake_cli(path: Path) -> Path:
    """写一个假的 pi CLI（由 ``sys.executable`` 直接执行，模拟 node + cli.js）。"""
    source = '''# -*- coding: utf-8 -*-
import json
import os
import sys
import time


def record(payload):
    path = os.environ.get("FAKE_PI_RECORD")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\\n")


ARGS = sys.argv[1:]
if "--version" in ARGS:
    print(os.environ.get("FAKE_PI_VERSION", "0.85.1"))
    sys.exit(0)

record({"argv": ARGS})
mode = "text"
if "--mode" in ARGS:
    mode = ARGS[ARGS.index("--mode") + 1]

if mode != "json":
    sys.exit(0)

stdin_text = sys.stdin.buffer.read().decode("utf-8", "replace")
record({"stdin": stdin_text})
sleep = float(os.environ.get("FAKE_PI_SLEEP", "0") or 0)
if sleep > 0:
    time.sleep(sleep)

fail_text = os.environ.get("FAKE_PI_FAIL", "")
if fail_text:
    sys.stderr.write(fail_text)
    sys.stderr.flush()
    sys.exit(1)

if os.environ.get("FAKE_PI_LLM_ERROR"):
    events = [
        {"type": "session", "version": 3, "id": "fake"},
        {"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}},
        {"type": "message_end", "message": {"role": "assistant", "content": [], "stopReason": "error",
                                            "errorMessage": "500 server_error: mock exploded"}},
        {"type": "agent_end", "messages": [], "willRetry": False},
        {"type": "agent_settled"},
    ]
else:
    text = os.environ.get("FAKE_PI_TEXT", "假 CLI 返回文本。")
    events = [
        {"type": "session", "version": 3, "id": "fake"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": text[:2]}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": text[2:]}},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                                            "stopReason": "stop", "usage": {"input": 1, "output": 2}}},
        {"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": text}],
                                            "stopReason": "stop"}]},
        {"type": "agent_settled"},
    ]
out = sys.stdout.buffer
for event in events:
    out.write((json.dumps(event, ensure_ascii=False) + "\\n").encode("utf-8"))
    out.flush()
sys.exit(0)
'''
    path.write_text(source, encoding="utf-8")
    return path


def _fake_node_env(monkeypatch, cli_path: Path) -> None:
    """让运行时把 ``sys.executable`` 当 node、把假脚本当 cli.js。"""
    monkeypatch.setenv("THESIS_NODE", sys.executable)
    monkeypatch.setenv("THESIS_PI_CLI", str(cli_path))
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)


def _wait_until(predicate, timeout: float, interval: float = 0.02) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


class _Collector:
    """线程安全的事件收集器（live 测试用）。"""

    def __init__(self) -> None:
        self.events: list[ar.AgentEvent] = []
        self.done = threading.Event()

    def __call__(self, event: ar.AgentEvent) -> None:
        self.events.append(event)
        if event.kind == "done":
            self.done.set()

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def deltas(self) -> str:
        return "".join(event.text for event in self.events if event.kind == "text_delta")

    def first(self, kind: str):
        for event in self.events:
            if event.kind == kind:
                return event
        return None

    def done_event(self):
        for event in reversed(self.events):
            if event.kind == "done":
                return event
        return None

    def wait_done(self, timeout: float = 60.0) -> bool:
        return self.done.wait(timeout)

    def wait_first_delta(self, timeout: float = 30.0) -> bool:
        return _wait_until(lambda: self.first("text_delta") is not None, timeout)


# --------------------------------------------------------------------------
# 1. 常量 / 数据类 / prompt 构造函数
# --------------------------------------------------------------------------


def test_public_constants_match_contract():
    assert ar.BASE_DIR == ROOT
    assert ar.DEFAULT_PROVIDER == "moonshotai-cn"
    assert ar.DEFAULT_MODEL == "kimi-k2-0905-preview"
    assert ar.AGENT_TOOLS == (
        "read,grep,ls,find,thesis_pdf_ingest,thesis_outline_validate,"
        "thesis_outline_save,thesis_save_polished"
    )
    if not os.environ.get("THESIS_PI_AGENT_DIR"):
        assert ar.AGENT_DIR == ROOT / ".pi" / "agent"
    if not os.environ.get("THESIS_SYSTEM_PROMPT"):
        assert ar.SYSTEM_PROMPT_PATH == ROOT / ".pi" / "prompts" / "thesis_system.md"


def test_agent_event_defaults_are_independent():
    first = ar.AgentEvent(kind="text_delta", text="你好")
    second = ar.AgentEvent(kind="status")
    assert first.kind == "text_delta" and first.text == "你好"
    assert second.text == "" and second.data == {}
    first.data["x"] = 1
    assert second.data == {}, "AgentEvent.data 默认值必须是独立 dict"


def test_env_overrides_resolution(monkeypatch, workdir):
    custom_agent = workdir / "custom-agent"
    custom_prompt = workdir / "custom-prompt.md"
    monkeypatch.setenv("THESIS_PI_AGENT_DIR", str(custom_agent))
    monkeypatch.setenv("THESIS_SYSTEM_PROMPT", str(custom_prompt))
    assert ar._resolve_agent_dir(ROOT) == custom_agent
    assert ar._resolve_system_prompt(ROOT) == custom_prompt
    # 实例化时读取环境变量，便于测试与运行时改配置。
    runtime = ar.PiRuntime(base_dir=workdir)
    assert runtime.agent_dir == custom_agent
    assert runtime.system_prompt_path == custom_prompt
    monkeypatch.delenv("THESIS_PI_AGENT_DIR")
    monkeypatch.delenv("THESIS_SYSTEM_PROMPT")
    assert ar._resolve_agent_dir(ROOT) == ROOT / ".pi" / "agent"


def test_provider_model_env_overrides(monkeypatch, workdir):
    """THESIS_PROVIDER / THESIS_MODEL 可覆盖 provider/model（RPC 与 JSON 模式共用）。"""
    # 默认值保持不变（契约常量）。
    assert ar.DEFAULT_PROVIDER == "moonshotai-cn"
    assert ar.DEFAULT_MODEL == "kimi-k2-0905-preview"
    if not os.environ.get("THESIS_PROVIDER"):
        assert ar.PROVIDER == ar.DEFAULT_PROVIDER
    if not os.environ.get("THESIS_MODEL"):
        assert ar.MODEL == ar.DEFAULT_MODEL

    monkeypatch.setenv("THESIS_PROVIDER", "moonshotai")
    monkeypatch.setenv("THESIS_MODEL", "kimi-k2.6")
    assert ar._resolve_provider() == "moonshotai"
    assert ar._resolve_model() == "kimi-k2.6"
    runtime = ar.PiRuntime(base_dir=workdir)
    assert runtime.provider == "moonshotai"
    assert runtime.model == "kimi-k2.6"
    argv = runtime._build_common_flags(workdir / "prompt.md", mode="rpc")
    assert argv[argv.index("--provider") + 1] == "moonshotai"
    assert argv[argv.index("--model") + 1] == "kimi-k2.6"
    # 空字符串视为未设置，回落到默认值。
    monkeypatch.setenv("THESIS_PROVIDER", "   ")
    monkeypatch.setenv("THESIS_MODEL", "")
    assert ar._resolve_provider() == "moonshotai-cn"
    assert ar._resolve_model() == "kimi-k2-0905-preview"


def test_build_outline_prompt_contract():
    pdf = Path(r"D:\papers\论文最终版.pdf")
    prompt = ar.build_outline_prompt(pdf)
    assert str(pdf) in prompt
    assert ".thesis/pdf_text.txt" in prompt
    for token in ("thesis_pdf_ingest", "thesis_outline_validate", "thesis_outline_save"):
        assert token in prompt
    assert "read" in prompt and "limit=2000" in prompt
    assert "outline.txt" in prompt
    for label in ("题目：", "摘要：", "第一章题目：", "第五章题目：", "总结：", "参考文献："):
        assert label in prompt
    assert "大纲" in prompt and "文件" in prompt
    assert "不要只在聊天回复里" in prompt  # 文件才是交付物


def test_build_polish_prompt_contract():
    position = "第三章 3.2 节"
    text = "这是一段需要润色的原文，包含术语与引用[1]。"
    outline = Path(r"D:\papers\outline.txt")
    prompt = ar.build_polish_prompt(position, text, outline)
    assert position in prompt
    assert text in prompt
    assert str(outline) in prompt
    assert "read" in prompt and "offset" in prompt
    assert ".thesis/pdf_text.txt" in prompt
    assert "thesis_save_polished" in prompt
    assert "polished.txt" in prompt
    assert "语义不变" in prompt and "不新增事实" in prompt
    assert "最终交付物是 polished.txt 文件" in prompt
    assert "聊天回复" in prompt


def test_build_chat_prompt_contract():
    prompt = ar.build_chat_prompt("请解释第三章的研究方法")
    assert "请解释第三章的研究方法" in prompt
    assert "outline.txt" in prompt and ".thesis/pdf_text.txt" in prompt
    assert "read" in prompt
    assert "thesis_save_polished" in prompt


# --------------------------------------------------------------------------
# 2. CLI 发现
# --------------------------------------------------------------------------


def test_find_node_env_override(monkeypatch, workdir):
    fake_node = workdir / "node.exe"
    fake_node.write_text("", encoding="utf-8")
    monkeypatch.setenv("THESIS_NODE", str(fake_node))
    assert ar.find_node() == fake_node.resolve()


def test_find_node_missing_message(monkeypatch, workdir):
    monkeypatch.setenv("THESIS_NODE", str(workdir / "nope.exe"))
    with pytest.raises(ar.PiUnavailable) as info:
        ar.find_node()
    assert "THESIS_NODE" in str(info.value) and "node" in str(info.value)


def test_find_node_which_fallback(monkeypatch):
    monkeypatch.delenv("THESIS_NODE", raising=False)

    def fake_which(name):
        return "C:\\Program Files\\nodejs\\node.exe" if name == "node" else None

    monkeypatch.setattr(ar.shutil, "which", fake_which)
    assert ar.find_node() == Path("C:\\Program Files\\nodejs\\node.exe")


def test_find_pi_cli_env_override(monkeypatch, workdir):
    fake_cli = workdir / "cli.js"
    fake_cli.write_text("// fake", encoding="utf-8")
    monkeypatch.setenv("THESIS_PI_CLI", str(fake_cli))
    assert ar.find_pi_cli() == fake_cli.resolve()


def test_find_pi_cli_env_override_missing(monkeypatch, workdir):
    monkeypatch.setenv("THESIS_PI_CLI", str(workdir / "missing-cli.js"))
    with pytest.raises(ar.PiUnavailable) as info:
        ar.find_pi_cli()
    assert "THESIS_PI_CLI" in str(info.value)


def test_find_pi_cli_from_which_pi(monkeypatch, workdir):
    npm_dir = workdir / "npm"
    cli = npm_dir / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fake cli", encoding="utf-8")
    shim = npm_dir / "pi.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    monkeypatch.delenv("THESIS_PI_CLI", raising=False)
    monkeypatch.setattr(ar.shutil, "which", lambda name: str(shim) if name == "pi" else None)
    assert ar.find_pi_cli() == cli.resolve()


def test_find_pi_cli_appdata_fallback(monkeypatch, workdir):
    appdata = workdir / "AppData-Roaming"
    cli = appdata / "npm" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// fake cli", encoding="utf-8")
    monkeypatch.delenv("THESIS_PI_CLI", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(ar.shutil, "which", lambda name: None)
    assert ar.find_pi_cli() == cli.resolve()


def test_find_pi_cli_missing_message(monkeypatch, workdir):
    monkeypatch.delenv("THESIS_PI_CLI", raising=False)
    monkeypatch.setenv("APPDATA", str(workdir / "no-appdata"))
    monkeypatch.setattr(ar.shutil, "which", lambda name: None)
    with pytest.raises(ar.PiUnavailable) as info:
        ar.find_pi_cli()
    assert "pi" in str(info.value) and "npm i -g" in str(info.value)


def test_launch_prefix():
    node = Path("C:/node/node.exe")
    js = Path("C:/npm/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js")
    cmd = Path("C:/npm/pi.cmd")
    assert ar._launch_prefix(node, js) == [str(node), str(js)]
    assert ar._launch_prefix(node, cmd) == [str(cmd)]


def test_check_available_with_fake_cli(monkeypatch, workdir):
    fake_cli = _write_fake_cli(workdir / "fake_pi.py")
    _fake_node_env(monkeypatch, fake_cli)
    runtime, _ = _isolated_runtime(workdir, monkeypatch)
    ok, message = runtime.check_available()
    assert ok is True, message
    assert "0.85.1" in message
    assert "node" in message


def test_check_available_reports_missing_node(monkeypatch, workdir):
    monkeypatch.setenv("THESIS_NODE", str(workdir / "missing-node.exe"))
    monkeypatch.delenv("THESIS_PI_CLI", raising=False)
    runtime, _ = _isolated_runtime(workdir, monkeypatch)
    ok, message = runtime.check_available()
    assert ok is False
    assert "node" in message and "THESIS_NODE" in message


def test_detect_pi_version_parses_output(monkeypatch, workdir):
    fake_cli = _write_fake_cli(workdir / "fake_pi.py")
    monkeypatch.setenv("FAKE_PI_VERSION", "0.85.1")
    version = ar.detect_pi_version(Path(sys.executable), fake_cli)
    assert version == "0.85.1"


# --------------------------------------------------------------------------
# 3. 启动前校验 / argv / env
# --------------------------------------------------------------------------


def test_start_rejects_empty_key(workdir):
    runtime = _simple_runtime(workdir)
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.start("   ")
    assert "密钥" in str(info.value)


def test_start_reports_missing_system_prompt(monkeypatch, workdir):
    fake_cli = _write_fake_cli(workdir / "fake_pi.py")
    _fake_node_env(monkeypatch, fake_cli)
    runtime, paths = _isolated_runtime(workdir, monkeypatch)
    paths["prompt"].unlink()
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.start("sk-fake")
    assert "系统提示词" in str(info.value)
    assert str(paths["prompt"]) in str(info.value)


def test_start_reports_missing_extension(monkeypatch, workdir):
    fake_cli = _write_fake_cli(workdir / "fake_pi.py")
    _fake_node_env(monkeypatch, fake_cli)
    runtime, paths = _isolated_runtime(workdir, monkeypatch, with_extension=False)
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.start("sk-fake")
    assert "扩展" in str(info.value)
    assert "thesis-tools.ts" in str(info.value)


def test_ask_requires_key(workdir):
    runtime = _simple_runtime(workdir)
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.ask("你好", api_key="")
    assert "密钥" in str(info.value)


def test_ask_rejects_empty_prompt(workdir):
    runtime = _simple_runtime(workdir)
    with pytest.raises(ar.PiRuntimeError):
        runtime.ask("   ", api_key="sk-fake")


def test_build_env_and_argv_hide_key(monkeypatch, workdir):
    runtime, paths = _isolated_runtime(workdir, monkeypatch)
    env = runtime._build_env("sk-super-secret")
    assert env["PI_CODING_AGENT_DIR"] == str(runtime.agent_dir)
    assert env["MOONSHOT_API_KEY"] == "sk-super-secret"
    assert env["PI_OFFLINE"] == "1"
    assert env["PYTHONIOENCODING"].lower() == "utf-8"
    assert env["THESIS_PYTHON"] == sys.executable
    assert "127.0.0.1" in env["NO_PROXY"]
    # 没有 key 时不应把空 key 传下去
    env_no_key = runtime._build_env(None)
    assert "MOONSHOT_API_KEY" not in env_no_key
    # argv 里绝不允许出现密钥 / prompt 正文
    argv = runtime._build_rpc_argv(Path(sys.executable), Path("C:/fake/cli.js"), paths["prompt"])
    joined = " ".join(argv)
    assert "sk-super-secret" not in joined
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "rpc"
    assert "--provider" in argv and argv[argv.index("--provider") + 1] == ar.DEFAULT_PROVIDER
    assert "--model" in argv and argv[argv.index("--model") + 1] == ar.DEFAULT_MODEL
    assert "--no-session" in argv
    assert "--offline" in argv
    assert argv[argv.index("--tools") + 1] == ar.AGENT_TOOLS
    assert argv[argv.index("--system-prompt") + 1] == str(paths["prompt"])


# --------------------------------------------------------------------------
# 4. 事件解析（fixture：pi 0.85.1 实测事件行）
# --------------------------------------------------------------------------


def test_text_stream_and_done(workdir):
    runtime = _simple_runtime(workdir)
    _mark_task_started(runtime)
    events = _feed(runtime, "rpc_text_events.jsonl")
    kinds = [event.kind for event in events]
    assert "text_delta" in kinds
    deltas = "".join(event.text for event in events if event.kind == "text_delta")
    assert deltas == "你好，这是离线 mock 回复。"
    assert runtime.last_assistant_text == "你好，这是离线 mock 回复。"
    done = next(event for event in events if event.kind == "done")
    assert kinds.index("done") == len(kinds) - 1, "done 必须是最后一个事件"
    assert done.data["ok"] is True
    assert done.data["error"] is None
    assert done.data["aborted"] is False
    assert done.data["text"] == "你好，这是离线 mock 回复。"
    assert done.data["usage"]["input"] == 10
    assert done.data["usage"]["output"] == 5
    assert runtime.busy is False
    assert "任务完成" in done.text


def test_done_data_contract_keys(workdir):
    """GUI 契约：done.data 必须含 text / usage / aborted（以及 ok / error）。"""
    runtime = _simple_runtime(workdir)
    _mark_task_started(runtime)
    events = _feed(runtime, "rpc_text_events.jsonl")
    done = next(event for event in events if event.kind == "done")
    for key in ("ok", "error", "usage", "text", "aborted"):
        assert key in done.data, f"done.data 缺少契约字段 {key}"
    assert isinstance(done.data["ok"], bool)
    assert done.data["error"] is None
    assert isinstance(done.data["usage"], dict) and done.data["usage"]
    assert isinstance(done.data["text"], str) and done.data["text"]
    assert isinstance(done.data["aborted"], bool) and done.data["aborted"] is False


def test_last_assistant_text_accumulates_while_streaming(workdir):
    runtime = _simple_runtime(workdir)
    _mark_task_started(runtime)
    collected: list[ar.AgentEvent] = []
    runtime.on_event = collected.append
    lines = [
        {"type": "message_start", "message": {"role": "assistant", "content": [], "stopReason": "pending"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "流式"}},
    ]
    for line in lines:
        for event in runtime._handle_line(json.dumps(line, ensure_ascii=False)):
            runtime._emit(event)
    assert runtime.last_assistant_text == "流式"
    assert runtime.busy is True
    assert any(event.kind == "text_delta" and event.text == "流式" for event in collected)
    runtime.stop()  # 清理（无进程，幂等）


def test_tool_events_mapping(workdir):
    runtime = _simple_runtime(workdir)
    _mark_task_started(runtime)
    events = _feed(runtime, "rpc_tool_events.jsonl")
    tool_start = next(event for event in events if event.kind == "tool_start")
    tool_updates = [event for event in events if event.kind == "tool_update"]
    tool_end = next(event for event in events if event.kind == "tool_end")
    assert tool_start.data["toolName"] == "read"
    assert tool_start.data["args"] == {"path": "big.txt"}
    assert tool_start.data["toolCallId"] == "call_read_1"
    assert tool_updates, "应映射 tool_execution_update"
    assert "50%" in tool_updates[0].text
    assert tool_updates[0].data["details"] == {"progress": 50}
    assert tool_end.data["toolName"] == "read"
    assert tool_end.data["is_error"] is False
    assert "文件内容示例" in tool_end.data["result_text"]
    assert "完成" in tool_end.text
    done = next(event for event in events if event.kind == "done")
    assert done.data["ok"] is True
    assert runtime.last_assistant_text == "工具执行完毕，任务完成。"


def test_tool_error_mapping(workdir):
    runtime = _simple_runtime(workdir)
    _mark_task_started(runtime)
    events = _feed(runtime, "rpc_tool_error_events.jsonl")
    tool_end = next(event for event in events if event.kind == "tool_end")
    assert tool_end.data["is_error"] is True
    assert "ENOENT" in tool_end.data["result_text"]
    assert "失败" in tool_end.text
    # 工具失败不应导致整个任务失败（pi 会把错误结果交给模型继续）
    done = next(event for event in events if event.kind == "done")
    assert done.data["ok"] is True


def test_llm_error_sets_done_false(workdir):
    runtime = _simple_runtime(workdir)
    _mark_task_started(runtime)
    events = _feed(runtime, "rpc_error_events.jsonl")
    kinds = [event.kind for event in events]
    assert "error" in kinds
    assert "status" in kinds  # auto_retry 状态也应上报
    done = next(event for event in events if event.kind == "done")
    assert done.data["ok"] is False
    assert done.data["error"]
    assert "模型服务" in done.data["error"] or "模型" in done.data["error"]
    assert runtime.busy is False


def test_abort_events_set_done_false(workdir):
    runtime = _simple_runtime(workdir)
    _mark_task_started(runtime)
    events = _feed(runtime, "rpc_abort_events.jsonl")
    done = next(event for event in events if event.kind == "done")
    assert done.data["ok"] is False
    assert done.data["aborted"] is True
    assert "取消" in (done.data["error"] or "")
    assert runtime.last_assistant_text == "慢"
    assert runtime.busy is False
    assert any(event.kind == "status" and "取消" in event.text for event in events)


def test_prompt_rejected_sets_done_false(workdir):
    runtime = _simple_runtime(workdir)
    _mark_task_started(runtime)
    collected: list[ar.AgentEvent] = []
    runtime.on_event = collected.append
    response = {
        "id": "p-rejected",
        "type": "response",
        "command": "prompt",
        "success": False,
        "error": "Agent is already processing. Specify streamingBehavior ('steer' or 'followUp') to queue the message.",
    }
    for event in runtime._handle_line(json.dumps(response, ensure_ascii=False)):
        runtime._emit(event)
    assert any(event.kind == "error" for event in collected)
    done = next(event for event in collected if event.kind == "done")
    assert done.data["ok"] is False
    assert "处理" in done.data["error"]
    assert runtime.busy is False


def test_response_id_correlation(workdir):
    runtime = _simple_runtime(workdir)
    response = {"id": "state-1", "type": "response", "command": "get_state", "success": True, "data": {"x": 1}}
    # 没有等待中的 future 时，未知 response 不应产生任何事件
    assert runtime._handle_line(json.dumps(response)) == []


def test_extension_ui_auto_confirm(workdir):
    runtime = _simple_runtime(workdir)
    sent: list[dict] = []
    runtime._send_obj = sent.append  # type: ignore[assignment]
    runtime.on_event = lambda event: None
    events = runtime._handle_line(_read_fixture("rpc_extension_ui_request.jsonl")[0])
    assert sent, "应自动回复 extension_ui_response"
    assert sent[0]["type"] == "extension_ui_response"
    assert sent[0]["id"] == "e604a413-fa57-4df9-879e-08a393208668"
    assert sent[0]["confirmed"] is True
    assert events and events[0].kind == "status"


def test_process_death_reports_error_and_done(workdir):
    runtime = _simple_runtime(workdir)
    _mark_task_started(runtime)
    collected: list[ar.AgentEvent] = []
    runtime.on_event = collected.append
    sentinel = object()
    runtime._proc = sentinel  # type: ignore[assignment]
    runtime._stderr_tail.append("Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)")
    runtime._on_reader_end(sentinel)  # type: ignore[arg-type]
    assert any(event.kind == "error" for event in collected)
    done = next(event for event in collected if event.kind == "done")
    assert done.data["ok"] is False
    assert "意外退出" in done.data["error"]
    assert runtime.busy is False


def test_new_session_response_emits_session_event(workdir):
    runtime = _simple_runtime(workdir)
    runtime.on_event = lambda event: None
    collected: list[ar.AgentEvent] = []
    runtime.on_event = collected.append
    response = {"id": "new-1", "type": "response", "command": "new_session", "success": True, "data": {"cancelled": False}}
    for event in runtime._handle_line(json.dumps(response)):
        runtime._emit(event)
    assert collected and collected[0].kind == "session"
    assert collected[0].data["new_session"] is True


# --------------------------------------------------------------------------
# 5. 错误文案 / 文本工具
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("No API key found for moonshotai-cn.", "密钥"),
        ('Unknown provider "x".', "0.85"),
        ("Agent is already processing.", "处理"),
        ("Request was aborted", "取消"),
        ("401 Unauthorized: invalid api key", "401"),
        ("429 rate_limit exceeded", "429"),
        ("500 server_error: overloaded", "模型服务"),
        ("fetch failed: ECONNREFUSED", "网络"),
    ],
)
def test_humanize_pi_error(raw, expected):
    assert expected in ar.humanize_pi_error(raw)


def test_content_text_extracts_text_blocks():
    assert ar._content_text("abc") == "abc"
    assert ar._content_text([{"type": "text", "text": "你"}, {"type": "text", "text": "好"}]) == "你好"
    assert ar._content_text([{"type": "toolCall", "id": "1"}]) == ""
    assert ar._content_text(None) == ""


# --------------------------------------------------------------------------
# 6. run_once_json（假 CLI：验证 argv/stdin/超时/取消/错误路径）
# --------------------------------------------------------------------------


def _json_fallback_runtime(monkeypatch, workdir, **fake_env):
    fake_cli = _write_fake_cli(workdir / "fake_pi.py")
    _fake_node_env(monkeypatch, fake_cli)
    runtime, paths = _isolated_runtime(workdir, monkeypatch)
    for key, value in fake_env.items():
        monkeypatch.setenv(key, value)
    record_path = workdir / "fake_record.jsonl"
    monkeypatch.setenv("FAKE_PI_RECORD", str(record_path))
    return runtime, record_path, paths


def test_run_once_json_uses_stdin_not_argv(monkeypatch, workdir):
    prompt = "请润色这段文字：😀 中文长文本。" * 30
    runtime, record_path, paths = _json_fallback_runtime(
        monkeypatch, workdir, FAKE_PI_TEXT="一次性 JSON 返回。"
    )
    text = runtime.run_once_json(prompt, api_key="sk-fake", timeout=60)
    assert text == "一次性 JSON 返回。"
    records = [json.loads(line) for line in record_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    argv = records[0]["argv"]
    stdin_text = records[1]["stdin"]
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "json"
    assert prompt not in " ".join(argv), "长 prompt 绝不能进 argv"
    assert stdin_text == prompt, "prompt 必须完整走 stdin"
    assert argv[argv.index("--provider") + 1] == ar.DEFAULT_PROVIDER
    assert argv[argv.index("--tools") + 1] == ar.AGENT_TOOLS
    assert "--system-prompt" in argv


def test_run_once_json_uses_env_provider_model(monkeypatch, workdir):
    """降级路径同样遵守 THESIS_PROVIDER / THESIS_MODEL 覆盖。"""
    monkeypatch.setenv("THESIS_PROVIDER", "moonshotai")
    monkeypatch.setenv("THESIS_MODEL", "kimi-k2.6")
    runtime, record_path, _ = _json_fallback_runtime(
        monkeypatch, workdir, FAKE_PI_TEXT="覆盖后的返回。"
    )
    assert runtime.run_once_json("测试模型覆盖", api_key="sk-fake", timeout=60) == "覆盖后的返回。"
    records = [
        json.loads(line)
        for line in record_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    argv = records[0]["argv"]
    assert argv[argv.index("--provider") + 1] == "moonshotai"
    assert argv[argv.index("--model") + 1] == "kimi-k2.6"


def test_run_once_json_timeout(monkeypatch, workdir):
    runtime, _, _ = _json_fallback_runtime(monkeypatch, workdir, FAKE_PI_SLEEP="10")
    started = time.monotonic()
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.run_once_json("测试超时", api_key="sk-fake", timeout=1.0)
    elapsed = time.monotonic() - started
    assert "超时" in str(info.value)
    assert elapsed < 8.0, f"超时路径耗时过长：{elapsed:.1f}s"


def test_run_once_json_cancel(monkeypatch, workdir):
    runtime, _, _ = _json_fallback_runtime(monkeypatch, workdir, FAKE_PI_SLEEP="10")
    cancel_event = threading.Event()

    def _cancel_later():
        time.sleep(0.3)
        cancel_event.set()

    threading.Thread(target=_cancel_later, daemon=True).start()
    started = time.monotonic()
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.run_once_json("测试取消", api_key="sk-fake", timeout=30, cancel_event=cancel_event)
    elapsed = time.monotonic() - started
    assert "取消" in str(info.value)
    assert elapsed < 8.0


def test_run_once_json_process_error(monkeypatch, workdir):
    runtime, _, _ = _json_fallback_runtime(
        monkeypatch, workdir, FAKE_PI_FAIL='Error: Unknown provider "bogus".'
    )
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.run_once_json("测试失败", api_key="sk-fake", timeout=30)
    assert "Unknown provider" in str(info.value) or "bogus" in str(info.value)


def test_run_once_json_llm_error(monkeypatch, workdir):
    runtime, _, _ = _json_fallback_runtime(monkeypatch, workdir, FAKE_PI_LLM_ERROR="1")
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.run_once_json("测试模型报错", api_key="sk-fake", timeout=30)
    assert "模型服务" in str(info.value) or "模型" in str(info.value)


def test_run_once_json_requires_prompt_file(monkeypatch, workdir):
    fake_cli = _write_fake_cli(workdir / "fake_pi.py")
    _fake_node_env(monkeypatch, fake_cli)
    runtime, paths = _isolated_runtime(workdir, monkeypatch)
    paths["prompt"].unlink()
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.run_once_json("x", api_key="sk-fake")
    assert "系统提示词" in str(info.value)


def test_run_once_json_requires_key(workdir):
    runtime = _simple_runtime(workdir)
    with pytest.raises(ar.PiRuntimeError) as info:
        runtime.run_once_json("x", api_key=" ")
    assert "密钥" in str(info.value)


# --------------------------------------------------------------------------
# 7. live 子进程测试（真实 node + pi RPC <-> 本机 mock OpenAI 服务）
# --------------------------------------------------------------------------


def _live_available() -> bool:
    if not MOCK_SCRIPT.is_file():
        return False
    try:
        ar.find_node()
        ar.find_pi_cli()
    except ar.PiUnavailable:
        return False
    return True


live = pytest.mark.skipif(
    not _live_available(),
    reason="需要本机 node + pi CLI + tests/mock_openai_server.py（离线 mock 服务）",
)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _MockServer:
    def __init__(self, proc: subprocess.Popen, port: int, log_path: Path) -> None:
        self.proc = proc
        self.port = port
        self.log_path = log_path

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass


@pytest.fixture()
def mock_factory(workdir):
    created: list[_MockServer] = []

    def start(**scenario_env: str) -> _MockServer:
        port = _free_port()
        log_path = workdir / f"mock-{port}.jsonl"
        env = dict(os.environ)
        env.update({"PYTHONIOENCODING": "utf-8", "MOCK_LOG": str(log_path)})
        env.update(scenario_env)
        proc = subprocess.Popen(
            [sys.executable, str(MOCK_SCRIPT), str(port)],
            cwd=str(workdir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
                raise RuntimeError(f"mock server 启动失败（退出码 {proc.returncode}）：{stderr[:800]}")
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            proc.kill()
            raise RuntimeError(f"mock server 端口 {port} 未就绪")
        server = _MockServer(proc, port, log_path)
        created.append(server)
        return server

    yield start
    for server in created:
        server.stop()


def _live_runtime(workdir: Path, monkeypatch, port: int, collector: _Collector) -> ar.PiRuntime:
    runtime, _ = _isolated_runtime(workdir, monkeypatch, on_event=collector)
    models = runtime.agent_dir / "models.json"
    models.parent.mkdir(parents=True, exist_ok=True)
    models.write_text(
        json.dumps(
            {"providers": {"moonshotai-cn": {"baseUrl": f"http://127.0.0.1:{port}/v1"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-fake-offline")
    return runtime


@live
def test_live_rpc_text_stream_done_busy(workdir, monkeypatch, mock_factory):
    server = mock_factory(MOCK_FINAL_TEXT="离线运行时可正常工作。")
    collector = _Collector()
    runtime = _live_runtime(workdir, monkeypatch, server.port, collector)
    try:
        ok, message = runtime.check_available()
        assert ok is True, message
        assert "0.85.1" in message
        runtime.ask("请只回复一句测试文本", api_key="sk-fake-offline")
        assert runtime.busy is True, "ask() 返回后 busy 必须立即为 True"
        assert collector.wait_done(60), f"等待 done 超时；已收到事件：{collector.kinds()}"
        assert collector.deltas() == "离线运行时可正常工作。"
        assert len([event for event in collector.events if event.kind == "text_delta"]) >= 2
        assert runtime.last_assistant_text == "离线运行时可正常工作。"
        assert runtime.busy is False
        done = collector.done_event()
        assert done.data["ok"] is True and done.data["error"] is None
        assert done.data["text"] == "离线运行时可正常工作。"
        assert done.data["usage"], "done 应携带 usage"
        ready = next(
            (event for event in collector.events if event.kind == "status" and event.data.get("phase") == "ready"),
            None,
        )
        assert ready is not None and "moonshotai-cn" in ready.text
        assert any(event.kind == "session" for event in collector.events)
    finally:
        runtime.stop()


@live
def test_live_env_model_override(workdir, monkeypatch, mock_factory):
    """THESIS_MODEL 覆盖生效：真实 pi 用内置目录里的 kimi-k2.6 跑通 mock 链路。"""
    monkeypatch.setenv("THESIS_PROVIDER", "moonshotai-cn")
    monkeypatch.setenv("THESIS_MODEL", "kimi-k2.6")
    server = mock_factory(MOCK_FINAL_TEXT="模型覆盖生效。")
    collector = _Collector()
    runtime = _live_runtime(workdir, monkeypatch, server.port, collector)
    try:
        assert runtime.provider == "moonshotai-cn"
        assert runtime.model == "kimi-k2.6"
        runtime.ask("请回复测试", api_key="sk-fake-offline")
        assert collector.wait_done(60), f"等待 done 超时：{collector.kinds()}"
        done = collector.done_event()
        assert done.data["ok"] is True, done.data
        assert collector.deltas() == "模型覆盖生效。"
        ready = next(
            (
                event
                for event in collector.events
                if event.kind == "status" and event.data.get("phase") == "ready"
            ),
            None,
        )
        assert ready is not None and "kimi-k2.6" in ready.text
    finally:
        runtime.stop()


@live
def test_live_rpc_tool_start_end(workdir, monkeypatch, mock_factory):
    steps = json.dumps(
        [{"name": "read", "arguments": {"path": "missing_live_probe.txt"}}], ensure_ascii=False
    )
    server = mock_factory(MOCK_TOOL_STEPS=steps, MOCK_FINAL_TEXT="工具执行后的最终回复。")
    collector = _Collector()
    runtime = _live_runtime(workdir, monkeypatch, server.port, collector)
    try:
        runtime.ask("请读取 missing_live_probe.txt", api_key="sk-fake-offline")
        assert collector.wait_done(60), f"等待 done 超时；已收到事件：{collector.kinds()}"
        tool_start = collector.first("tool_start")
        tool_end = collector.first("tool_end")
        assert tool_start is not None and tool_end is not None
        assert tool_start.data["toolName"] == "read"
        assert tool_start.data["args"] == {"path": "missing_live_probe.txt"}
        assert tool_end.data["toolName"] == "read"
        assert tool_end.data["is_error"] is True, tool_end.data
        assert "missing_live_probe" in tool_end.data["result_text"] or "ENOENT" in tool_end.data["result_text"]
        done = collector.done_event()
        assert done.data["ok"] is True
        assert collector.deltas() == "工具执行后的最终回复。"
        assert runtime.busy is False
    finally:
        runtime.stop()


@live
def test_live_rpc_abort_mid_stream(workdir, monkeypatch, mock_factory):
    server = mock_factory(
        MOCK_FINAL_TEXT="这是一段用于取消测试的慢速流式文本。", MOCK_DELAY="0.2"
    )
    collector = _Collector()
    runtime = _live_runtime(workdir, monkeypatch, server.port, collector)
    try:
        runtime.ask("SLOW 慢速输出，稍后取消", api_key="sk-fake-offline")
        assert collector.wait_first_delta(30), "未收到首个 text_delta"
        assert runtime.busy is True
        started = time.monotonic()
        runtime.abort()
        assert time.monotonic() - started < 1.0, "abort() 必须立即返回（不阻塞 UI）"
        assert collector.wait_done(30), f"abort 后未在 30s 内收到 done：{collector.kinds()}"
        done = collector.done_event()
        assert done.data["ok"] is False
        assert done.data["aborted"] is True
        assert "取消" in (done.data["error"] or "")
        assert runtime.busy is False
        partial = runtime.last_assistant_text
        assert partial and "这是一段用于取消测试的慢速流式文本。".startswith(partial)
    finally:
        runtime.stop()


@live
def test_live_rpc_stop_within_three_seconds(workdir, monkeypatch, mock_factory):
    server = mock_factory(MOCK_FINAL_TEXT="停止测试用慢速流式文本。", MOCK_DELAY="0.3")
    collector = _Collector()
    runtime = _live_runtime(workdir, monkeypatch, server.port, collector)
    try:
        runtime.ask("SLOW 停止测试", api_key="sk-fake-offline")
        assert collector.wait_first_delta(30), "未收到首个 text_delta"
        started = time.monotonic()
        runtime.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, f"stop() 耗时 {elapsed:.2f}s，超过 3s"
        assert runtime.busy is False
        started = time.monotonic()
        runtime.stop()  # 幂等
        assert time.monotonic() - started < 1.0
    finally:
        runtime.stop()


@live
def test_live_run_once_json(workdir, monkeypatch, mock_factory):
    server = mock_factory(MOCK_FINAL_TEXT="一次性 JSON 模式返回成功。")
    collector = _Collector()
    runtime = _live_runtime(workdir, monkeypatch, server.port, collector)
    try:
        text = runtime.run_once_json("请回复一句", api_key="sk-fake-offline", timeout=120)
        assert text == "一次性 JSON 模式返回成功。"
    finally:
        runtime.stop()
