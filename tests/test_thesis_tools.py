# -*- coding: utf-8 -*-
"""ThesisPolish pi 工具层离线测试（pytest）。

覆盖范围（全部不需要真实网络 / 真实 LLM 调用）：

1. ``tools/pdf_ingest.py``
   - 对本地模拟的 Moonshot Files API（线程 + 临时端口）跑通上传/下载；
   - 断言 stdout 只有一行 JSON、``.thesis/pdf_text.txt`` 落盘、标题检测正确；
   - 断言 sha1 缓存复用（第二次不再上传）与 ``--force`` 强制重新上传；
   - 断言缺 Key / HTTP 500 / 空正文 / 文件不存在时的中文错误与退出码。
2. pi 内置 Moonshot provider（pi 0.85.1）
   - ``--offline --list-models`` 能看到 ``moonshotai-cn`` + ``kimi-k2-0905-preview``；
   - ``pi auth check --provider moonshotai-cn --json`` 在有 fake key 时 ready、
     无 key 时 not_ready（使用临时 PI_CODING_AGENT_DIR，不污染真实配置）。
3. 项目本地扩展 ``.pi/agent/extensions/thesis-tools.ts``
   - 静态契约检查（4 个工具名、settings.json、system prompt 的九行模板）；
   - 端到端：把扩展放进临时 PI_CODING_AGENT_DIR/extensions，配合
     ``tests/mock_openai_server.py``（模拟 chat + Files API）跑 ``pi --mode json``，
     断言扩展被自动加载、工具出现在请求 schema 中、``tool_execution_end`` 成功、
     文件确实落盘。

运行::

    python -m pytest tests/test_thesis_tools.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
HELPER = REPO_ROOT / "tools" / "pdf_ingest.py"
AGENT_DIR = REPO_ROOT / ".pi" / "agent"
EXTENSION = AGENT_DIR / "extensions" / "thesis-tools.ts"
SETTINGS = AGENT_DIR / "settings.json"
SYSTEM_PROMPT = REPO_ROOT / ".pi" / "prompts" / "thesis_system.md"
MOCK_CHAT_SERVER = TESTS_DIR / "mock_openai_server.py"

OUTLINE_LABELS = [
    "题目",
    "摘要",
    "第一章题目",
    "第二章题目",
    "第三章题目",
    "第四章题目",
    "第五章题目",
    "总结",
    "参考文献",
]
GOOD_OUTLINE = "\n".join(
    [
        "题目：基于深度学习的文本润色方法研究",
        "摘要：未提供",
        "第一章题目：绪论",
        "第二章题目：相关工作",
        "第三章题目：方法",
        "第四章题目：未提供",
        "第五章题目：未提供",
        "总结：本文提出了一种面向学术写作的润色流程。",
        "参考文献：未提供",
    ]
)

# 模拟 Files API 返回的论文正文（用于 pdf_ingest / 扩展端到端测试）
FILES_TEXT = (
    "题目：基于深度学习的文本润色方法研究\n"
    "摘要：本文研究面向学术写作的自动润色方法。\n"
    "Abstract: This paper studies academic text polishing.\n"
    "第一章 绪论\n"
    "1.1 研究背景\n"
    "1.2.3 细分小节\n"
    "第二章 相关工作\n"
    "参考文献\n"
    "[1] 张三. 示例文献. 2020.\n"
)

SAMPLE_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"


# ---------------------------------------------------------------------------
# 可执行文件定位（缺失时相关测试 skip，而不是 fail）
# ---------------------------------------------------------------------------
def _find_node() -> Path | None:
    candidates = [
        os.environ.get("THESIS_NODE"),
        shutil.which("node"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def _find_pi_cli() -> Path | None:
    candidates = [
        os.environ.get("THESIS_PI_CLI"),
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules"
            / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    which = shutil.which("pi")
    if which:
        candidate = Path(which).with_name("pi.cmd")
        if candidate.is_file():
            # npm shim 可以跑，但测试里优先 node+cli.js；退而求其次直接用 shim。
            return candidate
    return None


NODE = _find_node()
PI_CLI = _find_pi_cli()
PI_AVAILABLE = NODE is not None and PI_CLI is not None
requires_pi = pytest.mark.skipif(not PI_AVAILABLE, reason="未找到 node.exe 或 pi cli.js")


def _pi_command() -> list[str]:
    assert NODE is not None and PI_CLI is not None
    if PI_CLI.suffix.lower() == ".cmd":
        return [str(PI_CLI)]
    return [str(NODE), str(PI_CLI)]


# ---------------------------------------------------------------------------
# 本地模拟 Moonshot Files API
# ---------------------------------------------------------------------------
class _MockFilesHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        pass

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/v1/files"):
            self._send_json({"error": {"message": f"unknown path {self.path}"}}, 404)
            return
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with server.state_lock:  # type: ignore[attr-defined]
            server.uploads += 1  # type: ignore[attr-defined]
            server.last_upload_bytes = len(raw)  # type: ignore[attr-defined]
            server.last_upload_has_purpose = b"file-extract" in raw  # type: ignore[attr-defined]
            file_id = f"file-mock-{server.uploads}"  # type: ignore[attr-defined]
            server.files[file_id] = server.files_text  # type: ignore[attr-defined]
            fail = server.fail_upload  # type: ignore[attr-defined]
        if fail:
            self._send_json({"error": {"message": "mock files api exploded", "type": "server_error"}}, 500)
            return
        self._send_json(
            {
                "id": file_id,
                "object": "file",
                "bytes": len(raw),
                "created_at": int(time.time()),
                "filename": "mock.pdf",
                "purpose": "file-extract",
            }
        )

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/v1/files/") and self.path.endswith("/content"):
            server = self.server
            parts = self.path.strip("/").split("/")
            file_id = parts[2] if len(parts) >= 4 else ""
            with server.state_lock:  # type: ignore[attr-defined]
                text = "" if server.empty_text else server.files.get(file_id, server.files_text)  # type: ignore[attr-defined]
            payload = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._send_json({"error": {"message": f"unknown path {self.path}"}}, 404)


class MockFilesServer:
    """临时端口上的 Files API mock（线程模式，测试结束 shutdown）。"""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _MockFilesHandler)
        self.httpd.daemon_threads = True
        self.httpd.files = {}  # type: ignore[attr-defined]
        self.httpd.files_text = FILES_TEXT  # type: ignore[attr-defined]
        self.httpd.fail_upload = False  # type: ignore[attr-defined]
        self.httpd.empty_text = False  # type: ignore[attr-defined]
        self.httpd.uploads = 0  # type: ignore[attr-defined]
        self.httpd.last_upload_bytes = 0  # type: ignore[attr-defined]
        self.httpd.last_upload_has_purpose = False  # type: ignore[attr-defined]
        self.httpd.state_lock = threading.Lock()  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def uploads(self) -> int:
        return int(self.httpd.uploads)  # type: ignore[attr-defined]

    @property
    def last_upload_has_purpose(self) -> bool:
        return bool(self.httpd.last_upload_has_purpose)  # type: ignore[attr-defined]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def files_server() -> MockFilesServer:
    server = MockFilesServer()
    try:
        yield server
    finally:
        server.close()


def _test_tmp_root() -> Path:
    """挑选一个当前进程真正可写的临时根目录。

    DSH 受限沙箱里系统临时目录可能只能创建顶层目录（子目录/文件写入被拒绝），
    此时退回仓库内已被 .gitignore 忽略的 ``.thesis/pytest-tmp``。
    """
    system_temp = Path(tempfile.gettempdir())
    try:
        probe = system_temp / f"thesis-tests-probe-{uuid.uuid4().hex[:12]}"
        probe.mkdir()  # 不要传 mode=0o700：受限沙箱下该目录会拒绝后续写入
        try:
            (probe / "sub").mkdir()
            (probe / "sub" / "probe.txt").write_text("ok", encoding="utf-8")
            return system_temp
        finally:
            shutil.rmtree(probe, ignore_errors=True)
    except OSError:
        fallback = REPO_ROOT / ".thesis" / "pytest-tmp"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


TEST_TMP_ROOT = _test_tmp_root()


@pytest.fixture()
def workdir() -> Path:
    """独立临时工作目录。

    不使用 pytest 内置 ``tmp_path``：它用 ``tempfile.mkdtemp``（mode=0o700）建目录，
    在 DSH 受限沙箱下这类目录后续写入会被拒绝；这里显式用默认 mode 创建并自行清理。
    """
    for _ in range(100):
        path = TEST_TMP_ROOT / f"thesis-tests-{uuid.uuid4().hex[:12]}"
        try:
            path.mkdir()
        except FileExistsError:
            continue
        try:
            yield path
        finally:
            shutil.rmtree(path, ignore_errors=True)
        return
    raise RuntimeError(f"无法在 {TEST_TMP_ROOT} 创建测试临时目录")


def _run_helper(
    server: MockFilesServer | None,
    cwd: Path,
    pdf: Path,
    *extra_args: str,
    api_key: str | None = "sk-test-fake",
    out: str = ".thesis",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MOONSHOT_BASE_URL"] = server.base_url if server else "http://127.0.0.1:1/v1"
    env.pop("MOONSHOT_API_KEY", None)
    if api_key is not None:
        env["MOONSHOT_API_KEY"] = api_key
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    command = [sys.executable, str(HELPER), "--pdf", str(pdf), "--out", out, *extra_args]
    return subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


def _helper_summary(process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """helper 的 stdout 契约：只有一行 JSON。"""
    lines = [line for line in process.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout 应为一行 JSON，实际 {len(lines)} 行：{process.stdout!r}"
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload


def _write_sample_pdf(directory: Path) -> Path:
    pdf = directory / "sample.pdf"
    pdf.write_bytes(SAMPLE_PDF_BYTES)
    return pdf


# ---------------------------------------------------------------------------
# pdf_ingest.py：成功路径 / 缓存 / 标题检测
# ---------------------------------------------------------------------------
def test_pdf_ingest_success_writes_text_and_json(files_server: MockFilesServer, workdir: Path) -> None:
    pdf = _write_sample_pdf(workdir)
    process = _run_helper(files_server, workdir, pdf)

    assert process.returncode == 0, process.stderr
    summary = _helper_summary(process)
    assert summary["ok"] is True
    assert summary["error"] is None
    assert summary["chars"] == len(FILES_TEXT)
    assert summary["lines"] == len(FILES_TEXT.splitlines())
    assert summary["text_path"].endswith("pdf_text.txt")
    assert Path(summary["text_path"]).is_file()
    assert Path(summary["text_path"]).read_text(encoding="utf-8") == FILES_TEXT
    assert summary["cached"] is False
    assert files_server.uploads == 1
    assert files_server.last_upload_has_purpose, "上传 multipart 必须带 purpose=file-extract"

    current_text = workdir / ".thesis" / "pdf_text.txt"
    assert current_text.read_text(encoding="utf-8") == FILES_TEXT
    cache_dir = workdir / ".thesis" / "pdf"
    cached_texts = list(cache_dir.glob("*.txt"))
    cached_meta = list(cache_dir.glob("*.meta.json"))
    assert len(cached_texts) == 1 and len(cached_meta) == 1
    meta = json.loads(cached_meta[0].read_text(encoding="utf-8"))
    assert meta["sha1"] in cached_texts[0].name
    assert meta["remote_file_id"].startswith("file-mock-")

    # 标题检测：摘要 / Abstract / 第X章 / 1.2.3 / 参考文献
    headings = summary["headings"]
    assert isinstance(headings, list) and headings
    titles = [heading["title"] for heading in headings]
    assert "摘要" in titles
    assert "Abstract" in titles
    assert "参考文献" in titles
    assert any("第一章" in title for title in titles)
    assert any(title.startswith("1.2.3") for title in titles)
    assert next(h for h in headings if h["title"] == "摘要")["line"] == 2
    assert next(h for h in headings if h["title"].startswith("1.2.3"))["level"] == 3


def test_pdf_ingest_cache_reuse_and_force(files_server: MockFilesServer, workdir: Path) -> None:
    pdf = _write_sample_pdf(workdir)

    first = _run_helper(files_server, workdir, pdf)
    second = _run_helper(files_server, workdir, pdf)
    assert first.returncode == 0 and second.returncode == 0
    assert _helper_summary(first)["cached"] is False
    assert _helper_summary(second)["cached"] is True
    assert files_server.uploads == 1, "sha1 缓存命中时不应重复上传"

    forced = _run_helper(files_server, workdir, pdf, "--force")
    assert forced.returncode == 0
    assert _helper_summary(forced)["cached"] is False
    assert files_server.uploads == 2, "--force 应强制重新上传"

    # 缓存内容被删除后应重新上传
    for cached in (workdir / ".thesis" / "pdf").iterdir():
        cached.unlink()
    rebuilt = _run_helper(files_server, workdir, pdf)
    assert rebuilt.returncode == 0
    assert _helper_summary(rebuilt)["cached"] is False
    assert files_server.uploads == 3


def test_pdf_ingest_phase_errors(files_server: MockFilesServer, workdir: Path) -> None:
    pdf = _write_sample_pdf(workdir)

    # 1) 缺少 API Key
    no_key = _run_helper(files_server, workdir, pdf, api_key=None)
    assert no_key.returncode == 1
    no_key_summary = _helper_summary(no_key)
    assert no_key_summary["ok"] is False
    assert "MOONSHOT_API_KEY" in no_key_summary["error"]

    # 2) 文件不存在（不访问网络）
    missing = _run_helper(files_server, workdir, workdir / "nope.pdf")
    assert missing.returncode == 1
    assert "找不到 PDF" in _helper_summary(missing)["error"]

    # 3) HTTP 500
    files_server.httpd.fail_upload = True  # type: ignore[attr-defined]
    http_error = _run_helper(files_server, workdir, pdf)
    assert http_error.returncode == 1
    http_summary = _helper_summary(http_error)
    assert http_summary["ok"] is False
    assert "HTTP 500" in http_summary["error"]

    # 4) 空正文
    files_server.httpd.fail_upload = False  # type: ignore[attr-defined]
    files_server.httpd.empty_text = True  # type: ignore[attr-defined]
    empty = _run_helper(files_server, workdir, pdf)
    assert empty.returncode == 1
    empty_summary = _helper_summary(empty)
    assert empty_summary["ok"] is False
    assert "为空" in empty_summary["error"]
    assert not (workdir / ".thesis" / "pdf_text.txt").exists()


def test_detect_headings_unit() -> None:
    module = _load_helper_module()
    headings = module.detect_headings(FILES_TEXT)
    found = {(heading["level"], heading["title"]) for heading in headings}
    assert (1, "摘要") in found
    assert (1, "Abstract") in found
    assert (1, "参考文献") in found
    assert any(level == 1 and title.startswith("第一章") for level, title in found)
    assert any(level == 2 and title.startswith("1.1") for level, title in found)
    assert any(level == 3 and title.startswith("1.2.3") for level, title in found)
    # 普通句子不应被误判为标题
    normal = "本文共有 3 个创新点，分别如下。\n随后讨论了模型结构与训练细节。\n"
    assert module.detect_headings(normal) == []
    # 空文本
    assert module.detect_headings("") == []
    # 行号从 1 开始
    assert next(h for h in headings if h["title"] == "摘要")["line"] == 2


def _load_helper_module() -> Any:
    spec = importlib.util.spec_from_file_location(f"thesis_pdf_ingest_{uuid.uuid4().hex}", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 项目配置文件契约
# ---------------------------------------------------------------------------
def test_extension_files_exist_and_match_contract() -> None:
    assert EXTENSION.is_file(), f"缺少扩展文件: {EXTENSION}"
    assert SETTINGS.is_file(), f"缺少 settings: {SETTINGS}"
    assert SYSTEM_PROMPT.is_file(), f"缺少 system prompt: {SYSTEM_PROMPT}"
    assert HELPER.is_file(), f"缺少 PDF 助手: {HELPER}"

    source = EXTENSION.read_text(encoding="utf-8")
    for tool_name in (
        "thesis_pdf_ingest",
        "thesis_outline_validate",
        "thesis_outline_save",
        "thesis_save_polished",
    ):
        assert f'name: "{tool_name}"' in source, f"扩展未注册工具 {tool_name}"
    # 不允许额外注册与 Python 运行时契约冲突的 thesis_* 工具
    assert source.count('name: "thesis_') == 4
    # 无 UI 对话框（headless 安全）
    assert "ctx.ui." not in source
    # 用了 TypeBox 与 pi ExtensionAPI 类型
    assert 'from "typebox"' in source
    assert "ExtensionAPI" in source

    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    assert settings["packages"] == []
    assert settings.get("enableInstallTelemetry") is False

    prompt = SYSTEM_PROMPT.read_text(encoding="utf-8")
    for label in OUTLINE_LABELS:
        assert label in prompt, f"system prompt 缺少大纲字段 {label}"
    assert "9 行" in prompt or "九行" in prompt or "恰好 **9 行**" in prompt
    for tool_name in (
        "thesis_pdf_ingest",
        "thesis_outline_validate",
        "thesis_outline_save",
        "thesis_save_polished",
    ):
        assert tool_name in prompt, f"system prompt 未提及工具 {tool_name}"


# ---------------------------------------------------------------------------
# pi 内置 provider（moonshotai-cn + kimi-k2-0905-preview）
# ---------------------------------------------------------------------------
def _clean_pi_env(agent_dir: Path, api_key: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_OFFLINE"] = "1"
    for name in ("MOONSHOT_API_KEY", "OPENAI_API_KEY", "MOONSHOT_BASE_URL"):
        env.pop(name, None)
    if api_key is not None:
        env["MOONSHOT_API_KEY"] = api_key
    env["NO_PROXY"] = (env.get("NO_PROXY", "") + ",127.0.0.1,localhost").strip(",")
    return env


@requires_pi
def test_pi_list_models_shows_builtin_moonshot_provider(workdir: Path) -> None:
    command = _pi_command() + [
        "--mode", "json",
        "--no-session",
        "--offline",
        "--provider", "moonshotai-cn",
        "--model", "kimi-k2-0905-preview",
        "--list-models", "kimi-k2-0905",
    ]
    process = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=_clean_pi_env(workdir, "sk-fake-test-key"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    output = process.stdout + process.stderr
    assert process.returncode == 0, output
    assert "moonshotai-cn" in output
    assert "kimi-k2-0905-preview" in output


@requires_pi
def test_pi_auth_check_builtin_provider(workdir: Path) -> None:
    base = _pi_command() + ["auth", "check", "--provider", "moonshotai-cn", "--json"]
    ready_dir = workdir / "ready"
    ready_dir.mkdir()

    ready = subprocess.run(
        base,
        cwd=str(REPO_ROOT),
        env=_clean_pi_env(ready_dir, "sk-fake-test-key"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert ready.returncode == 0, ready.stdout + ready.stderr
    payload = json.loads(ready.stdout.strip().splitlines()[-1])
    assert payload["status"] == "ready"
    assert payload["provider"] == "moonshotai-cn"
    assert payload["authType"] == "api_key"

    not_ready = subprocess.run(
        base,
        cwd=str(REPO_ROOT),
        env=_clean_pi_env(workdir, None),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert not_ready.returncode == 1
    payload = json.loads(not_ready.stdout.strip().splitlines()[-1])
    assert payload["status"] == "not_ready"
    assert payload["provider"] == "moonshotai-cn"


# ---------------------------------------------------------------------------
# 扩展端到端：临时 agent dir 自动加载 + mock OpenAI/Files 服务跑完整 agent loop
# ---------------------------------------------------------------------------
def _load_chat_mock_module(steps: list[dict[str, Any]], log_path: Path) -> tuple[Any, Any]:
    """按环境变量注入行为后，加载 tests/mock_openai_server.py 并启动临时端口服务。"""
    os.environ["MOCK_TOOL_STEPS"] = json.dumps(steps, ensure_ascii=False)
    os.environ["MOCK_FINAL_TEXT"] = "工具调用已完成。"
    os.environ["MOCK_FILES_TEXT"] = FILES_TEXT
    os.environ["MOCK_LOG"] = str(log_path)
    spec = importlib.util.spec_from_file_location(
        f"mock_openai_server_{uuid.uuid4().hex}", MOCK_CHAT_SERVER
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = module.serve(0)
    return module, server


def _prepare_workspace(workdir: Path, chat_port: int) -> tuple[Path, Path]:
    """临时 agent dir（扩展 + settings + models.json 指到 mock）+ 临时项目目录。"""
    agent_dir = workdir / "agent"
    (agent_dir / "extensions").mkdir(parents=True)
    shutil.copy2(EXTENSION, agent_dir / "extensions" / "thesis-tools.ts")
    shutil.copy2(SETTINGS, agent_dir / "settings.json")
    (agent_dir / "models.json").write_text(
        json.dumps(
            {"providers": {"moonshotai-cn": {"baseUrl": f"http://127.0.0.1:{chat_port}/v1"}}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    project_dir = workdir / "project"
    (project_dir / "tools").mkdir(parents=True)
    shutil.copy2(HELPER, project_dir / "tools" / "pdf_ingest.py")
    (project_dir / "sample.pdf").write_bytes(SAMPLE_PDF_BYTES)
    return agent_dir, project_dir


def _run_pi_agent_loop(
    workdir: Path,
    steps: list[dict[str, Any]],
    *,
    precreate_outline: bool = False,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], Path]:
    workdir.mkdir(parents=True, exist_ok=True)
    mock_module, mock_server = _load_chat_mock_module(steps, workdir / "mock_requests.jsonl")
    try:
        chat_port = int(mock_server.server_address[1])
        agent_dir, project_dir = _prepare_workspace(workdir, chat_port)
        if precreate_outline:
            (project_dir / "outline.txt").write_text("旧的大纲内容\n", encoding="utf-8")
        env = _clean_pi_env(agent_dir, "sk-fake-test-key")
        env["THESIS_PYTHON"] = sys.executable
        env["MOONSHOT_BASE_URL"] = f"http://127.0.0.1:{chat_port}/v1"
        env["PYTHONIOENCODING"] = "utf-8"
        command = _pi_command() + [
            "--mode", "json",
            "--no-session",
            "--offline",
            "--provider", "moonshotai-cn",
            "--model", "kimi-k2-0905-preview",
            "--tools",
            "read,grep,ls,find,thesis_pdf_ingest,thesis_outline_validate,"
            "thesis_outline_save,thesis_save_polished",
            "--system-prompt", str(SYSTEM_PROMPT),
            "请按照系统提示与工具说明完成当前任务。",
        ]
        process = subprocess.run(
            command,
            cwd=str(project_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        mock_state = requests_get_json(f"http://127.0.0.1:{chat_port}/_mock/state")
        return process, mock_state, project_dir
    finally:
        mock_server.shutdown()
        mock_server.server_close()
        # 清理 mock 注入的环境变量，避免影响其它测试
        for name in ("MOCK_TOOL_STEPS", "MOCK_FINAL_TEXT", "MOCK_FILES_TEXT", "MOCK_LOG"):
            os.environ.pop(name, None)


def requests_get_json(url: str) -> dict[str, Any]:
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 - 本地回环地址
        payload = json.loads(response.read().decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _events(process: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in process.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _find_tool_end(events: list[dict[str, Any]], tool_name: str) -> dict[str, Any]:
    ends = [
        event
        for event in events
        if event.get("type") == "tool_execution_end" and event.get("toolName") == tool_name
    ]
    assert ends, f"没有找到 {tool_name} 的 tool_execution_end 事件；事件类型: {sorted({e.get('type') for e in events})}"
    return ends[-1]


@requires_pi
def test_extension_autoload_and_pdf_ingest_tool(workdir: Path) -> None:
    process, mock_state, project_dir = _run_pi_agent_loop(
        workdir, [{"name": "thesis_pdf_ingest", "arguments": {"path": "sample.pdf"}}]
    )
    assert process.returncode == 0, process.stdout + process.stderr
    events = _events(process)

    # 扩展被自动加载：请求的 tools schema 里出现 4 个 thesis_* 工具
    last_request = mock_state.get("last_request") or {}
    tool_names = {tool.get("function", {}).get("name") for tool in last_request.get("tools", [])}
    assert {
        "thesis_pdf_ingest",
        "thesis_outline_validate",
        "thesis_outline_save",
        "thesis_save_polished",
    } <= tool_names, f"请求 tools 中缺少 thesis_* 工具: {sorted(tool_names)}"

    end = _find_tool_end(events, "thesis_pdf_ingest")
    assert end["isError"] is False, end
    summary = json.loads(end["result"]["content"][0]["text"])
    assert summary["ok"] is True
    assert summary["chars"] == len(FILES_TEXT)
    # onUpdate 进度回调接通（至少一个 tool_execution_update，detail 带 phase）
    updates = [
        event
        for event in events
        if event.get("type") == "tool_execution_update"
        and event.get("toolName") == "thesis_pdf_ingest"
    ]
    assert updates, "thesis_pdf_ingest 没有发出任何 onUpdate 进度事件"
    assert any(
        ((event.get("partialResult") or {}).get("details") or {}).get("phase") for event in updates
    )
    text_path = project_dir / ".thesis" / "pdf_text.txt"
    assert text_path.is_file(), "扩展工具没有把正文写入 .thesis/pdf_text.txt"
    assert text_path.read_text(encoding="utf-8") == FILES_TEXT
    # mock Files API 确实收到了上传
    assert mock_state.get("uploads", 0) == 1
    # agent 继续跑完并产生最终文本
    assert any(
        event.get("type") == "message_end"
        and (event.get("message") or {}).get("role") == "assistant"
        and any(block.get("type") == "text" for block in (event.get("message") or {}).get("content", []))
        for event in events
    )


@requires_pi
def test_extension_outline_validate_tool(workdir: Path) -> None:
    process, _mock_state, project_dir = _run_pi_agent_loop(
        workdir, [{"name": "thesis_outline_validate", "arguments": {"content": GOOD_OUTLINE}}]
    )
    assert process.returncode == 0, process.stdout + process.stderr
    end = _find_tool_end(_events(process), "thesis_outline_validate")
    assert end["isError"] is False, end
    details = end["result"].get("details") or {}
    assert details.get("valid") is True, details
    assert details.get("issues") == []
    # validate 不写文件
    assert not (project_dir / "outline.txt").exists()

    bad = GOOD_OUTLINE.replace("参考文献：未提供", "参考文献：")
    process, _mock_state, _project_dir = _run_pi_agent_loop(
        workdir / "bad", [{"name": "thesis_outline_validate", "arguments": {"content": bad}}]
    )
    assert process.returncode == 0, process.stdout + process.stderr
    end = _find_tool_end(_events(process), "thesis_outline_validate")
    details = end["result"].get("details") or {}
    assert details.get("valid") is False
    assert any("参考文献" in issue for issue in details.get("issues", []))


@requires_pi
def test_extension_tool_errors_are_marked_is_error(workdir: Path) -> None:
    """工具失败必须 throw（pi 扩展 API 没有 isError 返回字段）→ 事件 isError=true。"""
    process, _mock_state, project_dir = _run_pi_agent_loop(
        workdir, [{"name": "thesis_pdf_ingest", "arguments": {"path": "missing.pdf"}}]
    )
    assert process.returncode == 0, process.stdout + process.stderr
    end = _find_tool_end(_events(process), "thesis_pdf_ingest")
    assert end["isError"] is True, end
    error_text = end["result"]["content"][0]["text"]
    assert "找不到 PDF" in error_text
    assert not (project_dir / ".thesis" / "pdf_text.txt").exists()


@requires_pi
def test_extension_outline_save_tool(workdir: Path) -> None:
    process, _mock_state, project_dir = _run_pi_agent_loop(
        workdir,
        [{"name": "thesis_outline_save", "arguments": {"content": GOOD_OUTLINE}}],
        precreate_outline=True,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    end = _find_tool_end(_events(process), "thesis_outline_save")
    assert end["isError"] is False, end
    details = end["result"].get("details") or {}
    assert details.get("saved") is True
    assert (project_dir / "outline.txt").read_text(encoding="utf-8") == GOOD_OUTLINE + "\n"
    assert (project_dir / "outline.txt.bak").read_text(encoding="utf-8") == "旧的大纲内容\n"
    assert details.get("valid") is True
