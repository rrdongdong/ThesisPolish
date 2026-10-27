"""端到端离线集成测试：真实 pi 进程 + 本仓库扩展工具 + 本地 mock 服务。

不访问外网、不调用真实 Kimi API：
- `tests/mock_openai_server.py` 同时模拟 OpenAI Chat Completions 与 Moonshot Files API；
- 临时项目目录复制仓库里的 `.pi/agent/extensions/thesis-tools.ts`、`.pi/prompts/thesis_system.md`、
  `tools/pdf_ingest.py`，并把 pi 的 provider baseUrl 指向 mock 服务；
- 断言 pi 的 JSON 事件流里扩展工具真实执行、文件真实落盘。

运行：python -m pytest tests/test_pi_e2e.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_SERVER = REPO_ROOT / "tests" / "mock_openai_server.py"

PROVIDER = "moonshotai-cn"
MODEL = "kimi-k2-0905-preview"
TOOLS = (
    "read,grep,ls,find,thesis_pdf_ingest,"
    "thesis_outline_validate,thesis_outline_save,thesis_save_polished"
)

OUTLINE_TEXT = "\n".join(
    [
        "题目：基于深度学习的论文润色研究",
        "摘要：本文研究论文润色。",
        "第一章题目：绪论",
        "第二章题目：相关工作",
        "第三章题目：方法",
        "第四章题目：实验",
        "第五章题目：总结与展望",
        "总结：完成润色系统。",
        "参考文献：有",
    ]
)

PDF_TEXT = "\n".join(
    [
        "题目：基于深度学习的论文润色研究",
        "摘要：本文研究论文润色技术。",
        "第一章 绪论",
        "第二章 相关工作",
        "第三章 方法",
        "第四章 实验",
        "第五章 总结与展望",
        "参考文献",
    ]
)


# --------------------------------------------------------------------------- 环境探测
def _find_node() -> str | None:
    node = os.environ.get("THESIS_NODE") or shutil.which("node")
    return node if node and Path(node).exists() else None


def _find_pi_cli() -> str | None:
    """定位 pi 的 cli.js（与 agent_runtime.py 的查找逻辑保持一致）。"""
    env_cli = os.environ.get("THESIS_PI_CLI")
    candidates: list[Path] = []
    if env_cli:
        candidates.append(Path(env_cli))
    pi_cmd = shutil.which("pi")
    if pi_cmd:
        base = Path(pi_cmd).parent
        candidates += [
            base / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
            Path(pi_cmd).parent.parent / "lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js",
        ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata) / "npm/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
        )
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return None


NODE = _find_node()
PI_CLI = _find_pi_cli()
requires_pi = pytest.mark.skipif(
    NODE is None or PI_CLI is None,
    reason="需要本地 node + pi（@earendil-works/pi-coding-agent）才能跑端到端测试",
)


# --------------------------------------------------------------------------- fixtures
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _new_workdir() -> Path:
    """在仓库内创建默认权限的临时工作目录。

    注意：pytest 的 tmp_path/tempfile.mkdtemp 会用 0o700 创建目录，
    在当前 DSH Windows 沙箱里这类目录之后会无法写入/列出（WinError 5），
    因此这里统一用默认权限的 Path.mkdir()。
    """
    base = Path(os.environ.get("THESIS_TEST_TMP") or (REPO_ROOT / ".thesis" / "pytest-work"))
    base.mkdir(parents=True, exist_ok=True)
    work = base / f"e2e-{uuid.uuid4().hex[:12]}"
    work.mkdir()
    return work


@pytest.fixture()
def work() -> Path:
    path = _new_workdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _wait_http(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"mock 服务未能在 {timeout}s 内就绪: {url}")


@pytest.fixture()
def mock_server_factory(work: Path):
    """工厂 fixture：按测试需要（场景 env）启动独立 mock 服务进程。

    返回 start(env_extra) -> (base_url, port, log_path)；测试结束统一回收进程。
    """
    procs: list[subprocess.Popen] = []
    port_counter = {"n": 0}

    def start(env_extra: dict | None = None):
        port = _free_port()
        port_counter["n"] += 1
        log_path = work / f"mock_requests_{port_counter['n']}.jsonl"
        env = {**os.environ, "MOCK_LOG": str(log_path), **(env_extra or {})}
        proc = subprocess.Popen(
            [sys.executable, str(MOCK_SERVER), str(port)],
            cwd=str(work),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        procs.append(proc)
        base_url = f"http://127.0.0.1:{port}/v1"
        _wait_http(f"http://127.0.0.1:{port}/_mock/state")
        return base_url, port, log_path

    yield start
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _copy_agent_project(project: Path) -> Path:
    """把仓库里的 pi 配置/扩展/工具复制到临时项目目录。"""
    agent_dir = project / ".pi" / "agent"
    (agent_dir / "extensions").mkdir(parents=True)
    (project / ".pi" / "prompts").mkdir(parents=True)
    (project / "tools").mkdir(parents=True)

    shutil.copy2(REPO_ROOT / ".pi" / "agent" / "extensions" / "thesis-tools.ts",
                 agent_dir / "extensions" / "thesis-tools.ts")
    shutil.copy2(REPO_ROOT / ".pi" / "agent" / "settings.json", agent_dir / "settings.json")
    shutil.copy2(REPO_ROOT / ".pi" / "prompts" / "thesis_system.md",
                 project / ".pi" / "prompts" / "thesis_system.md")
    shutil.copy2(REPO_ROOT / "tools" / "pdf_ingest.py", project / "tools" / "pdf_ingest.py")
    (project / "thesis.pdf").write_bytes(b"%PDF-1.4 fake pdf bytes for mock upload\n%%EOF")
    return agent_dir


def _models_json(base_url: str) -> str:
    return json.dumps({"providers": {PROVIDER: {"baseUrl": base_url}}}, ensure_ascii=False)


def _mock_env(base_url: str) -> dict[str, str]:
    return {
        "PI_OFFLINE": "1",
        "MOONSHOT_API_KEY": "test-key",
        "MOONSHOT_BASE_URL": base_url,
        "THESIS_PYTHON": sys.executable,
        "PYTHONIOENCODING": "utf-8",
    }


def _run_pi_json(project: Path, agent_dir: Path, base_url: str, prompt: str) -> list[dict]:
    env = {**os.environ, **_mock_env(base_url)}
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    cmd = [
        NODE, PI_CLI,
        "--mode", "json",
        "--no-session",
        "--offline",
        "--provider", PROVIDER,
        "--model", MODEL,
        "--tools", TOOLS,
        "--system-prompt", str(project / ".pi" / "prompts" / "thesis_system.md"),
        prompt,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(project),
        env=env,
        capture_output=True,
        timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stdout = proc.stdout.decode("utf-8", errors="replace")
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    assert proc.returncode == 0, (
        f"pi 退出码 {proc.returncode}\nstdout:\n{stdout[-3000:]}\n"
        f"stderr:\n{proc.stderr.decode('utf-8', errors='replace')[-3000:]}"
    )
    return events


def _tool_results(events: list[dict]) -> dict[str, dict]:
    return {
        evt["toolName"]: evt
        for evt in events
        if evt.get("type") == "tool_execution_end"
    }


# --------------------------------------------------------------------------- 测试
@requires_pi
def test_outline_flow_with_extension_tools(work: Path, mock_server_factory):
    """第一步：agent 调 thesis_pdf_ingest（真实上传 mock Files API + Python helper），
    第二步：调 thesis_outline_save 落盘；断言文件内容与工具结果。"""
    steps = json.dumps(
        [
            {"name": "thesis_pdf_ingest", "arguments": {"path": "thesis.pdf"}},
            {"name": "thesis_outline_save", "arguments": {"content": OUTLINE_TEXT}},
        ],
        ensure_ascii=False,
    )
    base_url, port, log_path = mock_server_factory(
        {"MOCK_TOOL_STEPS": steps, "MOCK_FILES_TEXT": PDF_TEXT}
    )
    project = work / "proj"
    project.mkdir()
    agent_dir = _copy_agent_project(project)
    (agent_dir / "models.json").write_text(_models_json(base_url), encoding="utf-8")
    events = _run_pi_json(project, agent_dir, base_url, "请提取大纲并保存到 outline.txt")

    results = _tool_results(events)
    assert "thesis_pdf_ingest" in results, f"未执行 pdf_ingest；events={[e.get('type') for e in events]}"
    ingest = results["thesis_pdf_ingest"]
    assert ingest["isError"] is False, ingest["result"]
    ingest_text = "".join(
        part.get("text", "") for part in ingest["result"]["content"] if part.get("type") == "text"
    )
    ingest_payload = json.loads(ingest_text)
    assert ingest_payload["ok"] is True, ingest_payload
    assert Path(ingest_payload["text_path"]).read_text(encoding="utf-8") == PDF_TEXT

    assert "thesis_outline_save" in results, "未执行 outline_save"
    save = results["thesis_outline_save"]
    assert save["isError"] is False, save["result"]
    assert (project / "outline.txt").read_text(encoding="utf-8").strip() == OUTLINE_TEXT

    # 真实经过了 mock Files API 上传
    uploads = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert any(item.get("event") == "upload" for item in uploads), uploads


@requires_pi
def test_polish_flow_writes_polished_file(work: Path, mock_server_factory):
    """润色链路：扩展工具 thesis_save_polished 落盘 + 润色历史。"""
    polished = "本节在保持原意的前提下优化了学术表达，术语与引用标记保持不变。"
    steps = json.dumps(
        [{"name": "thesis_save_polished",
          "arguments": {"text": polished, "position": "第三章 3.2"}}],
        ensure_ascii=False,
    )
    base_url, port, log_path = mock_server_factory({"MOCK_TOOL_STEPS": steps})
    project = work / "proj"
    project.mkdir()
    agent_dir = _copy_agent_project(project)
    (agent_dir / "models.json").write_text(_models_json(base_url), encoding="utf-8")
    (project / "outline.txt").write_text(OUTLINE_TEXT + "\n", encoding="utf-8")
    events = _run_pi_json(project, agent_dir, base_url, "请润色第三章 3.2 节")

    results = _tool_results(events)
    assert "thesis_save_polished" in results, events
    save = results["thesis_save_polished"]
    assert save["isError"] is False, save["result"]
    assert (project / "polished.txt").read_text(encoding="utf-8").rstrip("\n") == polished
    history = (project / ".thesis" / "polish_history.jsonl").read_text(encoding="utf-8")
    assert "第三章 3.2" in history

# --------------------------------------------------------------------------- RPC 主路径
def _load_runtime_module(monkeypatch, agent_dir: Path, system_prompt: Path, base_url: str):
    """按临时项目环境重新加载 agent_runtime 模块（模块级常量从 env 读取）。"""
    monkeypatch.setenv("THESIS_PI_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("THESIS_SYSTEM_PROMPT", str(system_prompt))
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-fake-e2e")
    monkeypatch.setenv("MOONSHOT_BASE_URL", base_url)
    monkeypatch.setenv("THESIS_PYTHON", sys.executable)
    spec = importlib.util.spec_from_file_location(
        f"agent_runtime_e2e_{uuid.uuid4().hex[:8]}", REPO_ROOT / "agent_runtime.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses 需要能按 __module__ 反查模块
    spec.loader.exec_module(module)
    return module


def _event_tool_name(event) -> str:
    data = getattr(event, "data", {}) or {}
    return str(data.get("toolName") or data.get("name") or getattr(event, "text", ""))


@requires_pi
def test_rpc_primary_path_via_pi_runtime(work: Path, mock_server_factory, monkeypatch):
    """GUI 主路径（长驻 RPC）：PiRuntime 驱动 pi，扩展工具落盘 outline.txt。"""
    final_text = "大纲已生成并保存到 outline.txt。"
    steps = json.dumps(
        [
            {"name": "thesis_pdf_ingest", "arguments": {"path": "thesis.pdf"}},
            {"name": "thesis_outline_save", "arguments": {"content": OUTLINE_TEXT}},
        ],
        ensure_ascii=False,
    )
    base_url, port, log_path = mock_server_factory(
        {
            "MOCK_TOOL_STEPS": steps,
            "MOCK_FILES_TEXT": PDF_TEXT,
            "MOCK_FINAL_TEXT": final_text,
        }
    )
    project = work / "proj"
    project.mkdir()
    agent_dir = _copy_agent_project(project)
    (agent_dir / "models.json").write_text(_models_json(base_url), encoding="utf-8")

    runtime_module = _load_runtime_module(
        monkeypatch, agent_dir, project / ".pi" / "prompts" / "thesis_system.md", base_url
    )

    events = []
    lock = threading.Lock()
    done = threading.Event()

    def on_event(event):
        with lock:
            events.append(event)
        if event.kind == "done":
            done.set()

    runtime = runtime_module.PiRuntime(base_dir=project, on_event=on_event)
    try:
        ok, message = runtime.check_available()
        assert ok is True, message
        runtime.ask(
            runtime_module.build_outline_prompt(project / "thesis.pdf"),
            api_key="sk-fake-e2e",
        )
        assert runtime.busy is True, "ask() 之后 busy 必须为 True"
        assert done.wait(180), f"等待 done 超时；事件：{[e.kind for e in events]}"

        done_event = [e for e in events if e.kind == "done"][-1]
        assert done_event.data.get("ok") is True, done_event.data
        assert runtime.last_assistant_text == final_text
        assert runtime.busy is False

        tool_names = {_event_tool_name(e) for e in events if e.kind == "tool_end"}
        assert "thesis_pdf_ingest" in tool_names, tool_names
        assert "thesis_outline_save" in tool_names, tool_names
        assert (project / "outline.txt").read_text(encoding="utf-8").strip() == OUTLINE_TEXT
        assert any(e.kind == "text_delta" for e in events), "缺少流式 text_delta 事件"
        assert not [e for e in events if e.kind == "error"], [
            e.text for e in events if e.kind == "error"
        ]
    finally:
        runtime.stop()


@requires_pi
def test_rpc_model_override_is_sent_to_provider(work: Path, mock_server_factory, monkeypatch):
    """GUI「Agent 模型」下拉框：ask(model=...) 切换模型并真实传给 provider。"""
    final_text = "模型覆盖测试完成。"
    base_url, port, log_path = mock_server_factory({"MOCK_FINAL_TEXT": final_text})
    project = work / "proj"
    project.mkdir()
    agent_dir = _copy_agent_project(project)
    (agent_dir / "models.json").write_text(_models_json(base_url), encoding="utf-8")
    runtime_module = _load_runtime_module(
        monkeypatch, agent_dir, project / ".pi" / "prompts" / "thesis_system.md", base_url
    )
    events = []
    done = threading.Event()

    def on_event(event):
        events.append(event)
        if event.kind == "done":
            done.set()

    runtime = runtime_module.PiRuntime(base_dir=project, on_event=on_event)
    try:
        runtime.ask("只回复一句话", api_key="sk-fake", model="kimi-k2.5")
        assert done.wait(120), f"等待 done 超时；事件：{[e.kind for e in events]}"
        state = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/_mock/state", timeout=10
            ).read().decode("utf-8")
        )
        assert state["last_request"]["model"] == "kimi-k2.5", state["last_request"]
        assert runtime.model == "kimi-k2.5"
        done_event = [e for e in events if e.kind == "done"][-1]
        assert done_event.data.get("ok") is True, done_event.data
    finally:
        runtime.stop()

OUTLINE_WITH_SECTIONS = "\n".join(
    [
        "题目：REALM: An MLLM-Agent Framework",
        "摘要：本文提出 REALM。",
        "第一章题目：Introduction",
        "  1.1 Motivation",
        "  1.2 Contributions",
        "第二章题目：Related Works",
        "  2.1 3D Segmentation",
        "第三章题目：Methodology",
        "  3.1 Feature Field",
        "第四章题目：Experiments",
        "第五章题目：Conclusion",
        "总结：提出并验证 REALM 框架。",
        "参考文献：有",
    ]
)


@requires_pi
def test_outline_with_subheadings_validated_and_saved(work, mock_server_factory, monkeypatch):
    """扩展工具：带缩进小标题的大纲应校验通过，并原样写入 outline.txt。"""
    steps = json.dumps(
        [
            {"name": "thesis_outline_validate", "arguments": {"content": OUTLINE_WITH_SECTIONS}},
            {"name": "thesis_outline_save", "arguments": {"content": OUTLINE_WITH_SECTIONS}},
        ],
        ensure_ascii=False,
    )
    base_url, port, log_path = mock_server_factory({"MOCK_TOOL_STEPS": steps})
    project = work / "proj"
    project.mkdir()
    agent_dir = _copy_agent_project(project)
    (agent_dir / "models.json").write_text(_models_json(base_url), encoding="utf-8")
    events = _run_pi_json(project, agent_dir, base_url, "请提取大纲并包含每章下一级小标题")

    results = _tool_results(events)
    assert "thesis_outline_validate" in results, events
    validate = results["thesis_outline_validate"]
    assert validate["isError"] is False, validate["result"]
    validate_details = validate["result"].get("details") or {}
    assert validate_details.get("valid") is True, validate_details
    assert validate_details.get("subheadingCount", 0) >= 2, validate_details
    assert validate_details.get("sections"), validate_details

    assert "thesis_outline_save" in results, events
    save = results["thesis_outline_save"]
    assert save["isError"] is False, save["result"]
    saved = (project / "outline.txt").read_text(encoding="utf-8")
    assert "  1.1 Motivation" in saved and "  2.1 3D Segmentation" in saved
    assert (save["result"].get("details") or {}).get("subheadingCount", 0) >= 2
