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


def test_chat_omits_temperature_for_fixed_models(monkeypatch):
    """Kimi K2.5+/K3 只允许 temperature=1：客户端应主动省略 temperature 字段。"""
    captured: list[dict] = []

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002 - 对齐 requests 签名
        captured.append(dict(json or {}))
        return _Resp()

    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-x")
    monkeypatch.setattr(moonshot_chat.requests, "post", fake_post)

    assert moonshot_chat.chat_completion(
        "kimi-k2.6", [{"role": "user", "content": "hi"}], retry_base=0.01
    ) == "ok"
    assert "temperature" not in captured[0]

    assert moonshot_chat.chat_completion(
        "moonshot-v1-8k", [{"role": "user", "content": "hi"}], temperature=0.3, retry_base=0.01
    ) == "ok"
    assert captured[1]["temperature"] == 0.3


def test_chat_retries_without_temperature_on_400(monkeypatch):
    """若服务端返回 400 invalid temperature，应去掉 temperature 重试一次。"""
    calls: list[dict] = []

    class _Resp400:
        status_code = 400
        text = '{"error":{"message":"invalid temperature: only 1 is allowed for this model"}}'

    class _Resp200:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "retry-ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None):  # noqa: A002
        calls.append(dict(json or {}))
        return _Resp400() if len(calls) == 1 else _Resp200()

    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-x")
    monkeypatch.setattr(moonshot_chat.requests, "post", fake_post)

    text = moonshot_chat.chat_completion(
        "moonshot-v1-8k", [{"role": "user", "content": "hi"}], retry_base=0.01
    )
    assert text == "retry-ok"
    assert "temperature" in calls[0]
    assert "temperature" not in calls[1]


def test_generate_outline_prompt_requests_subheadings():
    import generate_outline

    prompt = generate_outline.OUTLINE_PROMPT
    assert "下一级小标题" in prompt
    assert "  1.1" in prompt  # 两个空格缩进示例
    assert "两个空格缩进" in prompt
