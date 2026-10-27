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

import gc
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
        # 关键顺序：先清控件并 gc（此时 Tk 仍存活），再销毁根窗口，
        # 避免 StringVar/IntVar 的 __del__ 在后续测试的 GC 中访问已销毁的 Tk
        # （否则会触发不可捕获警告 "main thread is not in main loop"）。
        _clear_root(root)
        gc.collect()
        try:
            root.destroy()
        except tk.TclError:
            pass
        gc.collect()


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


def test_gui_quota_cooldown_warning(gui_app):
    """上次 429 后 60 秒内再发起任务，GUI 应提示冷却（避免重试风暴继续占满 RPM 配额）。"""
    app, _root = gui_app
    app.api_key_var.set("sk-fake")
    app._agent_ready = True
    app._agent_busy = False

    class _FakeRuntime:
        busy = False

        def ask(self, prompt, *, api_key, model=None, provider=None):
            pass

    app._runtime = _FakeRuntime()
    app._last_quota_error_at = time.time() - 5  # 5 秒前刚被 429
    api_key = app._begin_agent_action("outline", clear_stream=True, clear_log=True)
    assert api_key == "sk-fake"
    log = app._get_text_content(app.agent_log_text)
    assert "距上次 429" in log and "60 秒" in log


def test_abort_watchdog_force_stops_runtime(gui_app):
    """abort 15s 未确认时应强制 stop() pi 运行时，避免后台重试永远占着进程。"""
    app, _root = gui_app
    stopped = {"count": 0}

    class _FakeRuntime:
        busy = True

        def stop(self):
            stopped["count"] += 1
            self.busy = False

    app._runtime = _FakeRuntime()
    app._agent_busy = True
    app._pending_action = "outline"
    app._abort_watchdog()
    assert stopped["count"] == 1
    assert app._agent_busy is False
    assert "强制结束" in app._get_text_content(app.agent_log_text)


def test_gui_quota_cooldown_auto_starts_after_wait(gui_app):
    """快捷任务在 429 冷却期内应自动等待剩余时间后开始，而不是立刻再次撞限流。"""
    app, root = gui_app
    app.api_key_var.set("sk-fake")
    app._agent_ready = True
    app._agent_busy = False
    asked: list[str] = []

    class _FakeRuntime:
        busy = False

        def check_available(self):
            return True, "fake pi 0.85.1 可用"

        def ask(self, prompt, *, api_key, model=None, provider=None):
            asked.append(prompt)

    app._runtime = _FakeRuntime()
    pdf = _workdir() / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    app.pdf_path_var.set(str(pdf))
    app._last_quota_error_at = time.time() - 59.0  # 约 1 秒后冷却结束
    app.agent_run_outline()
    assert asked == [], "冷却期内不应立刻发起请求"
    log = app._get_text_content(app.agent_log_text)
    assert "自动开始" in log

    assert _pump(root, lambda: bool(asked), timeout=5), "冷却结束后未自动开始"
    assert len(asked) == 1


def test_gui_doctor_button_runs_diagnostic(gui_app, monkeypatch):
    """GUI「诊断限流」按钮应调用 tools/moonshot_doctor.py 并把输出写入状态日志。"""
    app, root = gui_app
    import GUI as GUI_MODULE

    assert app.btn_doctor.cget("text") == "诊断限流"
    app.api_key_var.set("sk-fake")

    class _FakeProc:
        stdout = "诊断输出：模型列表 OK"
        stderr = ""
        returncode = 0

    def fake_run(cmd, **kwargs):
        assert any("moonshot_doctor.py" in str(part) for part in cmd)
        return _FakeProc()

    monkeypatch.setattr(GUI_MODULE.subprocess, "run", fake_run)
    app.run_doctor()
    assert _pump(root, lambda: not app._doctor_running), "诊断未完成"
    root.update()
    log = app._get_text_content(app.agent_log_text)
    assert "诊断输出：模型列表 OK" in log
    assert "诊断结束" in log


def test_gui_prints_full_raw_error_detail(gui_app):
    """429 时状态日志应额外打印完整原始错误，便于排查 RPM/项目配额。"""
    app, _root = gui_app
    import types

    event = types.SimpleNamespace(
        kind="status",
        text="模型调用出错：429",
        data={"phase": "model_error", "raw_error": "429: organization max RPM 100 exceeded detail XYZ"},
    )
    app._handle_agent_event(event)
    log = app._get_text_content(app.agent_log_text)
    assert "原始错误（完整）" in log
    assert "detail XYZ" in log


def test_gui_live_preview_updates_outline_and_structure(gui_app):
    """tool 参数流应实时把大纲写进编辑器，并同步刷新「章节/小标题」面板。"""
    import json as _json
    import types

    app, _root = gui_app
    app._handle_agent_event(
        types.SimpleNamespace(
            kind="toolcall_start",
            text="thesis_outline_save",
            data={"toolCallId": "c1", "toolName": "thesis_outline_save"},
        )
    )
    outline_text = (
        "题目：REALM\n"
        "摘要：未提供\n"
        "第一章题目：Introduction\n"
        "  1.1 Motivation\n"
        "  1.2 Contributions\n"
        "第二章题目：Related Works\n"
        "  2.1 3D Segmentation\n"
        "第三章题目：Methodology\n"
        "第四章题目：Experiments\n"
        "第五章题目：Conclusion\n"
        "总结：未提供\n"
        "参考文献：有\n"
    )
    payload = _json.dumps({"content": outline_text}, ensure_ascii=False)
    half = len(payload) // 2
    for chunk in (payload[:half], payload[half:]):
        app._handle_agent_event(
            types.SimpleNamespace(
                kind="toolcall_delta", text=chunk, data={"toolCallId": "c1"}
            )
        )
    app._handle_agent_event(
        types.SimpleNamespace(
            kind="toolcall_end",
            text="thesis_outline_save",
            data={"toolCallId": "c1", "toolName": "thesis_outline_save"},
        )
    )
    editor = app._get_text_content(app.outline_text)
    assert "第一章题目：Introduction" in editor
    assert "  1.1 Motivation" in editor
    structure = app._get_text_content(app.agent_structure_text)
    assert "第一章 Introduction" in structure
    assert "1.1 Motivation" in structure
    assert "共 5 章 / 3 条小标题" in structure


def test_gui_parse_outline_structure_ignores_unindented_noise(gui_app):
    app, _root = gui_app
    chapters = app._parse_outline_structure(
        "题目：X\n第一章题目：A\n  1.1 a1\n  1.2 a2\n第二章题目：B\n随便一行\n  2.1 b1\n"
    )
    assert [title for title, _ in chapters] == ["第一章 A", "第二章 B"]
    assert chapters[0][1] == ["1.1 a1", "1.2 a2"]
    assert chapters[1][1] == ["2.1 b1"]


def test_gui_outline_file_watcher_updates_editor(gui_app):
    """传统流程/agent 写 outline.txt 时，文件监听应实时把内容与结构载入 GUI。"""
    app, root = gui_app
    work = _workdir()
    path = work / "outline.txt"
    path.write_text("题目：旧\n摘要：未提供\n", encoding="utf-8")
    app.outline_path_var.set(str(path))
    app._agent_busy = True
    app._start_outline_watch()
    time.sleep(0.02)
    path.write_text(
        "题目：新论文\n摘要：未提供\n第一章题目：Intro\n  1.1 Motivation\n第二章题目：未提供\n"
        "第三章题目：未提供\n第四章题目：未提供\n第五章题目：未提供\n总结：未提供\n参考文献：有\n",
        encoding="utf-8",
    )
    assert _pump(
        root,
        lambda: "第二章题目" in app._get_text_content(app.outline_text),
        timeout=5,
    ), "文件监听未实时更新大纲"
    assert "1.1 Motivation" in app._get_text_content(app.agent_structure_text)
    app._agent_busy = False
    app._stop_outline_watch()
