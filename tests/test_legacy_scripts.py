"""旧版流程（单次 chat 调用）离线测试：generate_outline.py / polish.py / tunnels.

不需要真实 API Key：`tests/mock_openai_server.py` 同时模拟
- OpenAI Chat Completions（非流式，供 tools/moonshot_chat.py 使用）
- Moonshot Files API（供 tools/pdf_ingest.py 使用）
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "tools"
MOCK_SERVER = REPO_ROOT / "tests" / "mock_openai_server.py"

for path in (REPO_ROOT, TOOLS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import moonshot_chat  # noqa: E402

OUTLINE_TEXT = "\n".join(
    [
        "题目：REALM: An MLLM-Agent Framework",
        "摘要：本文提出 REALM。",
        "第一章题目：Introduction",
        "第二章题目：Related Works",
        "第三章题目：Methodology",
        "第四章题目：Experiments",
        "第五章题目：Conclusion",
        "总结：提出并验证 REALM 框架。",
        "参考文献：有",
    ]
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _workdir() -> Path:
    base = Path(os.environ.get("THESIS_TEST_TMP") or (REPO_ROOT / ".thesis" / "pytest-work"))
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"legacy-{uuid.uuid4().hex[:12]}"
    path.mkdir()
    return path


def _wait_http(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    raise RuntimeError(f"mock 服务未就绪: {url}")


@pytest.fixture()
def mock_api():
    """工厂 fixture：start(env_extra) -> (base_url, port, work)。"""
    work = _workdir()
    procs: list[subprocess.Popen] = []
    counter = {"n": 0}

    def start(env_extra: dict | None = None):
        counter["n"] += 1
        port = _free_port()
        env = {
            **os.environ,
            "MOCK_LOG": str(work / f"mock_{counter['n']}.jsonl"),
            **(env_extra or {}),
        }
        proc = subprocess.Popen(
            [sys.executable, str(MOCK_SERVER), str(port)],
            cwd=str(work),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        procs.append(proc)
        _wait_http(f"http://127.0.0.1:{port}/_mock/state")
        return f"http://127.0.0.1:{port}/v1", port, work

    yield start
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _set_env(monkeypatch, base_url: str) -> None:
    monkeypatch.setenv("MOONSHOT_BASE_URL", base_url)
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-fake-offline")


def test_chat_completion_against_mock(mock_api, monkeypatch):
    base_url, port, _work = mock_api({"MOCK_FINAL_TEXT": "你好，这是离线 mock 回复。"})
    _set_env(monkeypatch, base_url)
    content = moonshot_chat.chat_completion(
        "kimi-test", [{"role": "user", "content": "hi"}], retry_base=0.01
    )
    assert content == "你好，这是离线 mock 回复。"
    state = urllib.request.urlopen(f"http://127.0.0.1:{port}/_mock/state", timeout=5)
    assert '"chat_calls": 1' in state.read().decode("utf-8")


def test_chat_completion_retries_on_500(mock_api, monkeypatch):
    base_url, port, _work = mock_api(
        {"MOCK_FINAL_TEXT": "重试成功", "MOCK_FAIL_STREAK": "2"}
    )
    _set_env(monkeypatch, base_url)
    content = moonshot_chat.chat_completion(
        "kimi-test",
        [{"role": "user", "content": "hi"}],
        max_attempts=4,
        retry_base=0.05,
    )
    assert content == "重试成功"
    state = urllib.request.urlopen(f"http://127.0.0.1:{port}/_mock/state", timeout=5)
    assert '"chat_calls": 3' in state.read().decode("utf-8")


def test_chat_completion_http_error_raises(mock_api, monkeypatch):
    _base_url, port, _work = mock_api()
    _set_env(monkeypatch, f"http://127.0.0.1:{port}/does-not-exist")
    with pytest.raises(moonshot_chat.MoonshotChatError):
        moonshot_chat.chat_completion(
            "kimi-test", [{"role": "user", "content": "hi"}], retry_base=0.01
        )


def test_generate_outline_main_writes_outline(mock_api, monkeypatch):
    base_url, _port, work = mock_api(
        {"MOCK_FINAL_TEXT": OUTLINE_TEXT, "MOCK_FILES_TEXT": "论文正文……"}
    )
    _set_env(monkeypatch, base_url)
    project = work / "proj"
    project.mkdir()
    pdf = project / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    monkeypatch.chdir(project)

    import generate_outline

    code = generate_outline.main([str(pdf), "--model", "kimi-test", "--retry-base", "0.01"])
    assert code == 0
    assert (project / "outline.txt").read_text(encoding="utf-8") == OUTLINE_TEXT


def test_polish_main_writes_output(mock_api, monkeypatch):
    base_url, _port, work = mock_api({"MOCK_FINAL_TEXT": "这是润色后的文本。"})
    _set_env(monkeypatch, base_url)
    project = work / "proj"
    project.mkdir()
    (project / "outline.txt").write_text(OUTLINE_TEXT, encoding="utf-8")
    monkeypatch.chdir(project)

    import polish

    code = polish.main(
        [
            "--outline",
            "outline.txt",
            "--position",
            "第三章 3.2",
            "--text",
            "这是一段需要润色的原文。",
            "--output",
            "polished.txt",
            "--model",
            "kimi-test",
            "--retry-base",
            "0.01",
        ]
    )
    assert code == 0
    assert (project / "polished.txt").read_text(encoding="utf-8") == "这是润色后的文本。"


def test_moonshot_doctor_reports_checks(mock_api, monkeypatch, capsys):
    """诊断脚本：模型列表 / 额度 / 最小 chat 均应输出结果。"""
    base_url, _port, _work = mock_api(
        {"MOCK_MODELS": "kimi-k2.5,kimi-k2.6", "MOCK_FINAL_TEXT": "pong"}
    )
    _set_env(monkeypatch, base_url)
    import moonshot_doctor

    code = moonshot_doctor.main(["--model", "kimi-k2.5", "--timeout", "5"])
    output = capsys.readouterr().out
    assert code == 0
    assert "kimi-k2.5" in output
    assert "账户额度" in output
    assert "HTTP 200" in output
    assert "chat 调用成功" in output
