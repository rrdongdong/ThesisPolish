"""离线测试：模型发现与模型切换（Agent 模型下拉框 / 自动刷新）。

覆盖：
- ``agent_runtime.fetch_moonshot_models()`` 对 mock Moonshot API 的成功/空列表/HTTP 错误/网络错误；
- 模型 404 的中文错误文案包含「刷新模型」指引；
- GUI「刷新模型」按钮逻辑（后台线程 + 主线程回填 + 自动挑选可用模型）；
- 提交任务时把下拉框选中的模型传给 runtime；
- 模型 404 时自动刷新模型列表。

无需真实网络与 API Key：mock 服务由 ``tests/mock_openai_server.py`` 提供。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import agent_runtime  # noqa: E402

MOCK_SERVER = REPO_ROOT / "tests" / "mock_openai_server.py"

try:
    import tkinter as tk

    _TK_AVAILABLE = True
except Exception:  # noqa: BLE001 - 无 tkinter 的环境跳过 GUI 用例
    tk = None  # type: ignore[assignment]
    _TK_AVAILABLE = False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _workdir() -> Path:
    """沙箱安全的临时目录（默认权限；避免 pytest tmp_path 的 0o700 问题）。"""
    base = Path(os.environ.get("THESIS_TEST_TMP") or (REPO_ROOT / ".thesis" / "pytest-work"))
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"models-{uuid.uuid4().hex[:12]}"
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
    """启动 mock Moonshot API（/v1/models 返回默认模型），返回 (base_url, port, workdir)。"""
    work = _workdir()
    port = _free_port()
    env = {**os.environ, "MOCK_LOG": str(work / "requests.jsonl")}
    proc = subprocess.Popen(
        [sys.executable, str(MOCK_SERVER), str(port)],
        cwd=str(work),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        _wait_http(f"http://127.0.0.1:{port}/_mock/state")
        yield f"http://127.0.0.1:{port}/v1", port, work
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------------- fetch_moonshot_models
def test_fetch_moonshot_models_success(mock_api):
    base_url, _port, _work = mock_api
    models = agent_runtime.fetch_moonshot_models("sk-test", base_url=base_url)
    assert models == ["kimi-k2-0905-preview"], models


def test_fetch_moonshot_models_empty_raises():
    """服务器返回空 data 时应给出中文错误。"""

    class _EmptyHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = b'{"object":"list","data":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: D401 - 静默
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmptyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with pytest.raises(agent_runtime.PiRuntimeError) as excinfo:
            agent_runtime.fetch_moonshot_models(
                "sk-test", base_url=f"http://127.0.0.1:{port}/v1", timeout=5
            )
        assert "为空" in str(excinfo.value)
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_moonshot_models_http_error(mock_api):
    _base_url, port, _work = mock_api
    with pytest.raises(agent_runtime.PiRuntimeError) as excinfo:
        agent_runtime.fetch_moonshot_models(
            "sk-test", base_url=f"http://127.0.0.1:{port}/does-not-exist"
        )
    message = str(excinfo.value)
    assert "模型列表" in message or "HTTP" in message


def test_fetch_moonshot_models_network_error():
    port = _free_port()  # 未监听
    with pytest.raises(agent_runtime.PiRuntimeError) as excinfo:
        agent_runtime.fetch_moonshot_models(
            "sk-test", base_url=f"http://127.0.0.1:{port}/v1", timeout=2
        )
    assert "网络" in str(excinfo.value) or "模型列表" in str(excinfo.value)


def test_fetch_moonshot_models_requires_key():
    with pytest.raises(agent_runtime.PiRuntimeError) as excinfo:
        agent_runtime.fetch_moonshot_models("")
    assert "密钥" in str(excinfo.value)


def test_humanize_model_404_mentions_refresh():
    message = agent_runtime.humanize_pi_error(
        '404: {"message":"Not found the model kimi-k2-0905-preview or Permission denied",'
        '"type":"resource_not_found_error"}'
    )
    assert "刷新模型" in message and "404" in message


# --------------------------------------------------------------------------- 账户额度
def test_fetch_moonshot_balance_success(mock_api):
    base_url, _port, _work = mock_api
    data = agent_runtime.fetch_moonshot_balance("sk-test", base_url=base_url)
    assert data["available_balance"] == 42.5
    assert data["cash_balance"] == 32.5
    assert data["voucher_balance"] == 10.0


def test_fetch_moonshot_balance_requires_key():
    with pytest.raises(agent_runtime.PiRuntimeError) as excinfo:
        agent_runtime.fetch_moonshot_balance("")
    assert "密钥" in str(excinfo.value)


def test_fetch_moonshot_balance_http_error(mock_api):
    _base_url, port, _work = mock_api
    with pytest.raises(agent_runtime.PiRuntimeError):
        agent_runtime.fetch_moonshot_balance(
            "sk-test", base_url=f"http://127.0.0.1:{port}/does-not-exist"
        )


def test_humanize_429_mentions_balance_hint():
    message = agent_runtime.humanize_pi_error(
        '429: {"error":{"message":"rate limit exceeded","type":"rate_limit_error"}}'
    )
    assert "429" in message and "查询额度" in message
    assert "原始错误" in message


# --------------------------------------------------------------------------- GUI：模型下拉框
@pytest.fixture(scope="module")
def tk_root():
    """整个模块复用一个 Tk 根窗口。

    当前 Windows 会话里连续多次 ``tk.Tk()`` 偶发失败（"no display"/Tcl 状态问题），
    因此只在模块级创建一次，各用例共用并清理子控件。
    """
    if not _TK_AVAILABLE:
        pytest.skip("tkinter 不可用，跳过 GUI 用例")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"当前会话无法创建 Tk 窗口（无显示环境）：{exc}")
    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def _clear_root(root) -> None:
    for child in root.winfo_children():
        try:
            child.destroy()
        except tk.TclError:
            pass


@pytest.fixture()
def gui_app(tk_root, monkeypatch):
    """在复用的根窗口上创建一个 GUI（不进入 mainloop），用完清理控件。"""
    monkeypatch.setenv("THESIS_PI_AGENT_DIR", str(REPO_ROOT / ".pi" / "agent"))
    settings = REPO_ROOT / ".thesis" / "ui_settings.json"
    try:
        settings.unlink()
    except OSError:
        pass
    import GUI  # noqa: E402 - 延迟导入

    _clear_root(tk_root)
    app = GUI.ThesisGUI(tk_root)
    tk_root.update_idletasks()
    try:
        yield app, tk_root
    finally:
        # 取消 GUI 的 after 轮询，避免下个用例收到已销毁控件的回调
        app._closing = True
        for job in (
            getattr(app, "_poll_job", None),
            getattr(app, "_ui_poll_job", None),
            getattr(app, "_probe_job", None),
        ):
            if job is None:
                continue
            try:
                tk_root.after_cancel(job)
            except (tk.TclError, ValueError):
                pass
        runtime = getattr(app, "_runtime", None)
        if runtime is not None:
            try:
                runtime.stop()
            except Exception:  # noqa: BLE001 - 测试清理不应失败
                pass
        _clear_root(tk_root)
        try:
            settings.unlink()
        except OSError:
            pass


def _pump(root, predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_gui_refresh_models_populates_combo(gui_app, monkeypatch):
    app, root = gui_app
    app.api_key_var.set("sk-fake")
    app.model_var.set("kimi-k2-0905-preview")
    fake_models = ["moonshot-v1-8k", "kimi-k2.5", "kimi-k2-0905-preview"]
    monkeypatch.setattr(
        agent_runtime, "fetch_moonshot_models", lambda key: list(fake_models)
    )
    app.refresh_models()
    assert _pump(root, lambda: not app._models_loading), "模型列表刷新超时"
    root.update()
    assert app.model_var.get() == "kimi-k2-0905-preview"  # 当前模型在列表内保持不变
    assert set(app.model_combo.cget("values")) == set(fake_models)


def test_gui_refresh_models_auto_switches_when_current_missing(gui_app, monkeypatch):
    app, root = gui_app
    app.api_key_var.set("sk-fake")
    app.model_var.set("kimi-k2-0905-preview")
    monkeypatch.setattr(
        agent_runtime, "fetch_moonshot_models", lambda key: ["moonshot-v1-8k", "kimi-k2.5"]
    )
    app.refresh_models()
    assert _pump(root, lambda: not app._models_loading), "模型列表刷新超时"
    root.update()
    assert app.model_var.get() == "kimi-k2.5", app.model_var.get()


def test_gui_submit_passes_selected_model(gui_app):
    app, _root = gui_app
    captured: dict = {}

    class _FakeRuntime:
        busy = False

        def ask(self, prompt, *, api_key, model=None, provider=None):
            captured.update(
                {"prompt": prompt, "api_key": api_key, "model": model, "provider": provider}
            )

    app._runtime = _FakeRuntime()
    app._agent_ready = True
    app.model_var.set("kimi-k2.5")
    assert app._submit_agent_prompt("测试 prompt", "sk-fake", "测试") is True
    assert captured["model"] == "kimi-k2.5"
    assert captured["api_key"] == "sk-fake"


def test_gui_model_404_triggers_auto_refresh(gui_app, monkeypatch):
    app, root = gui_app
    app.api_key_var.set("sk-fake")
    app.model_var.set("kimi-k2-0905-preview")
    app._model_choices = []
    called = {"count": 0}

    def fake_fetch(key):
        called["count"] += 1
        return ["kimi-k2.5", "moonshot-v1-8k"]

    monkeypatch.setattr(agent_runtime, "fetch_moonshot_models", fake_fetch)
    app._maybe_refresh_models_after_model_error(
        '404: {"message":"Not found the model kimi-k2-0905-preview or Permission denied",'
        '"type":"resource_not_found_error"}'
    )
    assert _pump(root, lambda: not app._models_loading), "自动刷新模型列表超时"
    root.update()
    assert called["count"] == 1
    assert app.model_var.get() == "kimi-k2.5"


def test_gui_check_balance_logs(gui_app, monkeypatch):
    app, root = gui_app
    app.api_key_var.set("sk-fake")
    monkeypatch.setattr(
        agent_runtime,
        "fetch_moonshot_balance",
        lambda key: {
            "available_balance": 12.5,
            "cash_balance": 2.5,
            "voucher_balance": 10.0,
        },
    )
    app.check_balance()
    assert _pump(root, lambda: not app._balance_loading), "查询额度超时"
    root.update()
    log = app._get_text_content(app.agent_log_text)
    assert "账户额度" in log and "12.50" in log


def test_runtime_provisions_aux_bins(monkeypatch):
    """rg/fd 缺失时从全局 pi 配置（~/.pi/agent/bin）复制到项目配置目录。"""
    work = _workdir()
    fake_home = work / "home"
    global_bin = fake_home / ".pi" / "agent" / "bin"
    global_bin.mkdir(parents=True)
    exe = ".exe" if os.name == "nt" else ""
    (global_bin / f"rg{exe}").write_bytes(b"fake-rg")
    (global_bin / f"fd{exe}").write_bytes(b"fake-fd")
    monkeypatch.setattr(agent_runtime.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(agent_runtime.shutil, "which", lambda name: None)

    agent_dir = work / "agent"
    runtime = agent_runtime.PiRuntime(base_dir=work)
    runtime.agent_dir = agent_dir
    runtime._ensure_aux_tool_bins()

    bin_dir = agent_dir / "bin"
    assert (bin_dir / f"rg{exe}").read_bytes() == b"fake-rg"
    assert (bin_dir / f"fd{exe}").read_bytes() == b"fake-fd"


def test_humanize_429_reports_org_rpm_limit():
    message = agent_runtime.humanize_pi_error(
        '429: {"message":"Your account org-c04770fae26844b6bc55c66ba8de6301 request '
        'reached organization max RPM: 3, please try again later"}'
    )
    assert "3 RPM" in message
    assert "传统流程" in message


def test_gui_recovers_saved_outline_when_final_step_failed(gui_app):
    """agent 已保存 outline.txt，但最后汇报调用 429 失败：GUI 应载入文件并提示实际完成。"""
    app, _root = gui_app
    work = _workdir()
    outline_path = work / "outline.txt"
    app.outline_path_var.set(str(outline_path))
    app._pending_action = "outline"
    app._action_started_at = time.time() - 5
    outline_path.write_text(
        "题目：REALM\n摘要：未提供\n第一章题目：Introduction\n第二章题目：Related Works\n"
        "第三章题目：Methodology\n第四章题目：Experiments\n第五章题目：Conclusion\n"
        "总结：未提供\n参考文献：有\n",
        encoding="utf-8",
    )
    app._handle_agent_done(
        {"ok": False, "error": "429: resource_exhausted quota", "usage": {}, "text": ""}
    )
    log = app._get_text_content(app.agent_log_text)
    assert "任务实际已完成" in log
    assert "题目：REALM" in app._get_text_content(app.outline_text)
