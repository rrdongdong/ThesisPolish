"""ThesisPolish GUI —— pi agent 助手 + 传统流程兼容。

两个标签页：
1. 「Agent 助手（pi）」：嵌入 pi v0.85.1 agent 运行时（agent_runtime.py），
   实时显示流式回复、工具调用与错误；提供「Agent 提取大纲 / Agent 润色当前段落」
   快捷流程和自由多轮对话。
2. 「传统流程（兼容）」：保留原有 generate_outline.py / polish.py 子进程流程；
   agent 运行时不可用时仍可正常使用。

线程模型（见 docs/pi-agent-integration.md §2/§5）：
    pi 读线程 --on_event--> queue.Queue --root.after(80ms) 轮询--> tkinter 主线程
on_event 回调运行在读线程里，因此只做 queue.put，绝不触碰 tk 控件。
"""

from __future__ import annotations

import functools
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent

# 保证无论从哪个工作目录启动，都能找到与 GUI.py 同目录的 agent_runtime.py
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# ---------------------------------------------------------------------------
# pi agent 运行时（agent_runtime.py 由 runtime owner 并行实现）。
# 导入失败时 GUI 必须能降级运行：Agent 标签页给出明确中文提示，传统流程照常工作。
# ---------------------------------------------------------------------------
AGENT_IMPORT_ERROR: str | None = None
_agent_runtime = None
try:
    import agent_runtime as _agent_runtime  # type: ignore[no-redef]
except Exception as exc:  # noqa: BLE001 - 缺少依赖/文件时也要让 GUI 启动
    _agent_runtime = None
    AGENT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

# 轮询间隔：50-100ms 批量刷新，避免每个 delta 一次 UI 抖动（pi-runtime.md §8）
POLL_INTERVAL_MS = 80
MAX_EVENTS_PER_POLL = 400
WINDOW_TITLE = "论文大纲提取与段落润色"
PI_BADGE = "pi 0.85.1 · Kimi · v2.3(2026-10-27)"

# pi 不可用 / 启动失败时的中文排查指引
AGENT_GUIDANCE = (
    "排查建议：\n"
    "1) 先切换到「传统流程（兼容）」标签页，旧版流程无需 pi 即可继续使用；\n"
    "2) 安装/升级 pi：npm i -g @earendil-works/pi-coding-agent；\n"
    "3) 确认已安装 Node.js（node 在 PATH 中，或设置 THESIS_NODE / THESIS_PI_CLI）；\n"
    "4) 检查 .pi/agent 配置与 .pi/prompts/thesis_system.md 是否存在。"
)


class ThesisGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.base_dir = BASE_DIR
        self._closing = False

        # --- agent 运行时状态（只在主线程读写；on_event 只写队列） ---
        self._agent_queue: "queue.Queue" = queue.Queue()
        self._ui_calls: "queue.Queue" = queue.Queue()
        self._poll_job: str | None = None
        self._ui_poll_job: str | None = None
        self._probe_job: str | None = None
        self._runtime = None
        self._runtime_init_error: str | None = None
        self._agent_ready: bool | None = None  # None = 尚未探测
        self._agent_available_reason = ""
        self._agent_busy = False
        self._pending_action: str | None = None  # "outline" / "polish" / "chat"
        self._action_started_at: float | None = None  # 本轮任务开始时间（判断文件是否本轮落盘）
        self._stream_buffer = ""  # 本轮已显示的助手 delta 累积
        self._last_agent_error = ""
        self._tool_update_seen: dict[str, str] = {}
        self._system_prompt_logged = False
        self._idle_since: float | None = None
        self._last_quota_error_at: float | None = None  # 上次 429 时间（用于冷却提示）
        self._cooldown_job: str | None = None  # 冷却等待的 after 任务 id
        self._cooldown_waiting = False  # 是否正在等待冷却后自动开始
        self._banner_wrap = 0

        # --- 两个标签页共享的 StringVars（顶部公共区） ---
        self.api_key_var = tk.StringVar()
        self.model_var = tk.StringVar(value=self._default_model())
        self._models_loading = False
        self._balance_loading = False
        self._model_choices: list[str] = []
        self.pdf_path_var = tk.StringVar(value=str(self.base_dir / "test2.pdf"))
        self.outline_path_var = tk.StringVar(value=str(self.base_dir / "outline.txt"))
        self.output_path_var = tk.StringVar(value=str(self.base_dir / "polished.txt"))
        self.position_var = tk.StringVar()

        self._build_ui()
        self._load_ui_settings()
        self._init_agent_runtime()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_job = self.root.after(POLL_INTERVAL_MS, self._poll_agent_events)
        # 工作线程 → 主线程的通用回调队列（refresh_models 等；不直接跨线程调用 Tk）
        self._ui_poll_job = self.root.after(60, self._poll_ui_calls)
        # 启动后再探测 pi 可用性，让窗口先显示出来（不阻塞 GUI 构建）
        self._probe_job = self.root.after(120, self._probe_agent_availability)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.title(WINDOW_TITLE)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # 初始尺寸随屏幕自适应，避免在小屏上超出可见区域
        try:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            width = min(1160, max(960, screen_w - 80))
            height = min(880, max(640, screen_h - 120))
            self.root.geometry(f"{width}x{height}")
            self.root.minsize(900, 640)
        except tk.TclError:
            pass

        self._build_header()

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.agent_tab = ttk.Frame(self.notebook)
        self.legacy_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.agent_tab, text="Agent 助手（pi）")
        self.notebook.add(self.legacy_tab, text="传统流程（兼容）")
        self._build_agent_tab(self.agent_tab)
        self._build_legacy_tab(self.legacy_tab)
        self.notebook.select(self.agent_tab)

    def _build_header(self) -> None:
        """顶部公共区：两个标签页共享的路径/密钥输入。"""
        header = ttk.Frame(self.root)
        header.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 6))
        header.columnconfigure(1, weight=1)

        title = ttk.Label(
            header,
            text="ThesisPolish · 论文大纲提取与段落润色",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        title.grid(row=0, column=0, columnspan=2, sticky="w")
        # 小状态标签：pi 0.85.1 · Kimi（探测后追加可用性）
        self.header_badge = ttk.Label(header, text=PI_BADGE, foreground="#6b7280")
        self.header_badge.grid(row=0, column=2, columnspan=2, sticky="e")

        ttk.Label(header, text="kimi API 密钥").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 2)
        )
        self.api_key_entry = ttk.Entry(header, textvariable=self.api_key_var, show="*")
        self.api_key_entry.grid(row=1, column=1, columnspan=3, sticky="we", pady=(6, 2))

        ttk.Label(header, text="PDF 路径").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(header, textvariable=self.pdf_path_var).grid(
            row=2, column=1, columnspan=2, sticky="we", pady=2
        )
        ttk.Button(header, text="选择 PDF", command=self.pick_pdf).grid(
            row=2, column=3, sticky="we", padx=(8, 0), pady=2
        )

        ttk.Label(header, text="大纲文件").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(header, textvariable=self.outline_path_var).grid(
            row=3, column=1, columnspan=3, sticky="we", pady=2
        )

        ttk.Label(header, text="润色输出").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(header, textvariable=self.output_path_var).grid(
            row=4, column=1, columnspan=3, sticky="we", pady=2
        )

        ttk.Label(header, text="润色位置").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(header, textvariable=self.position_var).grid(
            row=6, column=1, columnspan=3, sticky="we", pady=(2, 6)
        )

        # Agent 模型：可编辑下拉框 + 拉取当前 API Key 可用模型列表
        ttk.Label(header, text="Agent 模型").grid(
            row=5, column=0, sticky="w", padx=(0, 8), pady=2
        )
        self.model_combo = ttk.Combobox(
            header,
            textvariable=self.model_var,
            values=(self.model_var.get(),),
        )
        self.model_combo.grid(row=5, column=1, columnspan=2, sticky="we", pady=2)
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        self.btn_refresh_models = ttk.Button(
            header, text="刷新模型", command=self.refresh_models
        )
        self.btn_refresh_models.grid(row=5, column=3, sticky="we", padx=(8, 0), pady=2)
        self.btn_balance = ttk.Button(header, text="查询额度", command=self.check_balance)
        self.btn_balance.grid(row=5, column=4, sticky="we", padx=(6, 0), pady=2)

    # ------------------------------------------------------------------
    # Agent 助手标签页
    # ------------------------------------------------------------------
    def _build_agent_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        # 运行时可用性横幅（中文指引）
        self.agent_banner = ttk.Label(
            tab,
            text="正在检测 pi 运行时…",
            justify="left",
            foreground="#92400e",
        )
        self.agent_banner.grid(row=0, column=0, sticky="we", padx=8, pady=(8, 4))
        tab.bind("<Configure>", self._on_agent_tab_configure)

        # 快捷操作
        toolbar = ttk.Frame(tab)
        toolbar.grid(row=1, column=0, sticky="we", padx=8, pady=(0, 4))
        ttk.Label(toolbar, text="快捷操作：", foreground="#374151").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.btn_agent_outline = ttk.Button(
            toolbar, text="Agent 提取大纲", command=self.agent_run_outline
        )
        self.btn_agent_polish = ttk.Button(
            toolbar, text="Agent 润色当前段落", command=self.agent_run_polish
        )
        self.btn_load_outline = ttk.Button(
            toolbar, text="载入大纲到编辑器", command=self.load_outline_to_editor
        )
        self.btn_load_output = ttk.Button(
            toolbar, text="载入输出到输出面板", command=self.load_output_to_panel
        )
        quick_buttons = (
            self.btn_agent_outline,
            self.btn_agent_polish,
            self.btn_load_outline,
            self.btn_load_output,
        )
        for index, button in enumerate(quick_buttons, start=1):
            button.grid(row=0, column=index, sticky="w", padx=(0, 6))

        # 状态日志 + 流式输出（上下可拖动分栏）
        paned = ttk.Panedwindow(tab, orient="vertical")
        paned.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 4))

        log_frame = ttk.LabelFrame(paned, text="状态日志（只读：阶段 / 工具调用 / 错误）")
        stream_frame = ttk.LabelFrame(paned, text="流式输出（只读：助手回复）")
        paned.add(log_frame, weight=1)
        paned.add(stream_frame, weight=2)

        self.agent_log_text = self._make_text_with_scroll(log_frame, height=8, readonly=True)
        self.agent_stream_text = self._make_text_with_scroll(stream_frame, height=11, readonly=True)
        for tag, color in (
            ("error", "#b91c1c"),
            ("ok", "#166534"),
            ("tool", "#1d4ed8"),
            ("muted", "#6b7280"),
        ):
            self.agent_log_text.tag_configure(tag, foreground=color)

        # 输入区：发送 / 停止 / 新对话
        input_frame = ttk.LabelFrame(tab, text="输入（Ctrl+Enter 发送）")
        input_frame.grid(row=3, column=0, sticky="we", padx=8, pady=(0, 8))
        input_frame.columnconfigure(0, weight=1)
        self.agent_input_text = tk.Text(input_frame, height=4, wrap="word", undo=True)
        self.agent_input_text.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=(6, 2))
        input_scroll = ttk.Scrollbar(
            input_frame, orient="vertical", command=self.agent_input_text.yview
        )
        input_scroll.grid(row=0, column=1, sticky="ns", pady=(6, 2))
        self.agent_input_text.configure(yscrollcommand=input_scroll.set)
        self.agent_input_text.bind("<Control-Return>", self._on_agent_input_ctrl_enter)

        button_bar = ttk.Frame(input_frame)
        button_bar.grid(row=1, column=0, columnspan=2, sticky="we", padx=6, pady=(0, 6))
        ttk.Label(
            button_bar,
            text="Enter 换行；Ctrl+Enter 发送。「停止」可中止当前任务。",
            foreground="#6b7280",
        ).pack(side="left")
        self.btn_agent_new = ttk.Button(button_bar, text="新对话", command=self.agent_new_session)
        self.btn_agent_new.pack(side="right", padx=(6, 0))
        self.btn_agent_stop = ttk.Button(
            button_bar, text="停止", command=self.agent_stop, state="disabled"
        )
        self.btn_agent_stop.pack(side="right", padx=(6, 0))
        self.btn_agent_send = ttk.Button(button_bar, text="发送", command=self.agent_send)
        self.btn_agent_send.pack(side="right")

        # busy 时需要禁用的动作按钮（载入类本地按钮保持可用）
        self._agent_action_buttons = [
            self.btn_agent_outline,
            self.btn_agent_polish,
            self.btn_agent_send,
            self.btn_agent_new,
        ]

    def _on_agent_tab_configure(self, event: tk.Event) -> None:
        """跟随标签页宽度调整横幅换行宽度。"""
        wrap = max(360, int(event.width) - 32)
        if wrap != self._banner_wrap:
            self._banner_wrap = wrap
            try:
                self.agent_banner.configure(wraplength=wrap)
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # 传统流程（兼容）标签页 —— 原有控件与行为保持不变
    # ------------------------------------------------------------------
    def _build_legacy_tab(self, tab: ttk.Frame) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(3, weight=3)
        tab.rowconfigure(4, weight=2)
        tab.rowconfigure(6, weight=2)
        tab.rowconfigure(7, weight=1)

        note = ttk.Label(
            tab,
            text=(
                "兼容模式：直接调用 generate_outline.py / polish.py（单次模型调用，"
                "只依赖 requests 与 MOONSHOT_API_KEY，不需要 openai 包），不依赖 pi 运行时。"
            ),
            justify="left",
            foreground="#6b7280",
        )
        note.grid(row=0, column=0, sticky="we", padx=8, pady=(8, 2))

        self.btn_legacy_outline = ttk.Button(tab, text="提取论文大纲", command=self.run_outline)
        self.btn_legacy_outline.grid(row=1, column=0, sticky="we", padx=8, pady=(4, 6))

        load_save_row = ttk.Frame(tab)
        load_save_row.grid(row=2, column=0, sticky="we", padx=8, pady=(0, 6))
        load_save_row.columnconfigure(0, weight=1)
        load_save_row.columnconfigure(1, weight=1)
        ttk.Button(load_save_row, text="加载大纲", command=self.load_outline_to_editor).grid(
            row=0, column=0, sticky="we"
        )
        ttk.Button(load_save_row, text="保存大纲", command=self.save_outline_from_editor).grid(
            row=0, column=1, sticky="we", padx=(6, 0)
        )

        outline_frame = ttk.LabelFrame(tab, text="大纲可视化（可编辑）")
        outline_frame.grid(row=3, column=0, sticky="nsew", padx=8, pady=(0, 6))
        self.outline_text = self._make_text_with_scroll(outline_frame, height=7)

        input_frame = ttk.LabelFrame(tab, text="待润色文字")
        input_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=(0, 6))
        self.input_text = self._make_text_with_scroll(input_frame, height=6)

        self.btn_legacy_polish = ttk.Button(tab, text="开始润色", command=self.run_polish)
        self.btn_legacy_polish.grid(row=5, column=0, sticky="we", padx=8, pady=(0, 6))

        output_frame = ttk.LabelFrame(tab, text="润色后输出")
        output_frame.grid(row=6, column=0, sticky="nsew", padx=8, pady=(0, 6))
        self.output_text = self._make_text_with_scroll(output_frame, height=6)

        status_frame = ttk.LabelFrame(tab, text="状态")
        status_frame.grid(row=7, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.status_text = self._make_text_with_scroll(status_frame, height=4)

    # ------------------------------------------------------------------
    # 通用控件工具
    # ------------------------------------------------------------------
    @staticmethod
    def _make_text_with_scroll(
        parent: tk.Widget, height: int = 8, readonly: bool = False, width: int = 80
    ) -> tk.Text:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        text = tk.Text(parent, height=height, width=width, wrap="word", undo=not readonly)
        text.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 6), pady=6)
        text.configure(yscrollcommand=scrollbar.set)
        if readonly:
            text.configure(state="disabled")
        return text

    @staticmethod
    def _set_text_content(widget: tk.Text, content: str, *, readonly: bool = False) -> None:
        """整体替换 Text 内容（readonly=True 时恢复只读状态）。"""
        try:
            if readonly:
                widget.configure(state="normal")
            widget.delete("1.0", "end")
            if content:
                widget.insert("1.0", content)
            widget.see("end")
            if readonly:
                widget.configure(state="disabled")
        except tk.TclError:
            pass

    @staticmethod
    def _get_text_content(widget: tk.Text) -> str:
        try:
            return widget.get("1.0", "end")
        except tk.TclError:
            return ""

    def _resolve_path(self, raw: str, default_name: str) -> Path:
        """把界面上的路径解析成本地绝对路径；相对路径按项目根目录解析。"""
        raw = (raw or "").strip()
        if not raw:
            return self.base_dir / default_name
        try:
            path = Path(raw).expanduser()
        except (OSError, ValueError):
            return self.base_dir / default_name
        if not path.is_absolute():
            path = self.base_dir / path
        return path

    def _outline_path(self) -> Path:
        return self._resolve_path(self.outline_path_var.get(), "outline.txt")

    def _output_path(self) -> Path:
        return self._resolve_path(self.output_path_var.get(), "polished.txt")

    def _warn(self, title: str, message: str) -> None:
        if self._closing:
            return
        try:
            messagebox.showwarning(title, message, parent=self.root)
        except tk.TclError:
            pass

    def _show_error(self, title: str, message: str) -> None:
        if self._closing:
            return
        try:
            messagebox.showerror(title, message, parent=self.root)
        except tk.TclError:
            pass

    def _call_ui(self, func, *args) -> None:
        """从工作线程把 UI 操作排入主线程执行（不直接跨线程调用 Tk）。"""
        if self._closing:
            return
        try:
            self._ui_calls.put(functools.partial(func, *args))
        except Exception:  # noqa: BLE001 - 关闭过程中队列异常直接忽略
            pass

    def _poll_ui_calls(self) -> None:
        """每 60ms 在主线程执行工作线程排队的 UI 回调。"""
        if self._closing:
            return
        self._ui_poll_job = None
        try:
            for _ in range(MAX_EVENTS_PER_POLL):
                try:
                    func = self._ui_calls.get_nowait()
                except queue.Empty:
                    break
                try:
                    func()
                except Exception as exc:  # noqa: BLE001 - 单个回调异常不能打断轮询
                    self.append_agent_log(
                        f"⚠️ UI 回调失败：{type(exc).__name__}: {exc}", tag="error"
                    )
        finally:
            if not self._closing:
                try:
                    self._ui_poll_job = self.root.after(60, self._poll_ui_calls)
                except tk.TclError:
                    self._ui_poll_job = None

    # ------------------------------------------------------------------
    # 原有能力：选择 PDF / 大纲读写 / 状态日志
    # ------------------------------------------------------------------
    def pick_pdf(self) -> None:
        path = filedialog.askopenfilename(
            title="选择论文PDF",
            filetypes=[("PDF文件", "*.pdf"), ("所有文件", "*.*")],
        )
        if path:
            self.pdf_path_var.set(path)

    def append_status(self, text: str) -> None:
        try:
            self.status_text.insert("end", text + "\n")
            self.status_text.see("end")
        except tk.TclError:
            pass

    def load_outline_to_editor(self) -> None:
        path = self._outline_path()
        if not path.exists():
            self._warn("文件不存在", f"未找到大纲文件: {path}")
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._show_error("读取失败", f"无法读取大纲文件: {path}\n{exc}")
            return
        self._set_text_content(self.outline_text, content)
        self.append_status(f"已加载大纲: {path}")

    def save_outline_from_editor(self) -> bool:
        path = self._outline_path()
        content = self.outline_text.get("1.0", "end").strip()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content + ("\n" if content else ""), encoding="utf-8")
        except OSError as exc:
            self._show_error("保存失败", f"无法保存大纲文件: {path}\n{exc}")
            return False
        self.append_status(f"已保存大纲: {path}")
        return True

    def load_output_to_panel(self) -> None:
        """快捷操作：把润色输出文件载入「润色后输出」面板。"""
        path = self._output_path()
        if not path.exists():
            self._warn("文件不存在", f"未找到润色输出文件:\n{path}")
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._show_error("读取失败", f"无法读取润色输出文件: {path}\n{exc}")
            return
        self._set_text_content(self.output_text, content)
        self.append_status(f"已载入润色输出: {path}")

    # ------------------------------------------------------------------
    # 传统流程（兼容）：子进程调用旧脚本，行为与之前保持一致
    # ------------------------------------------------------------------
    def _run_subprocess(self, cmd: list[str], on_success=None) -> None:
        env = os.environ.copy()
        env["MOONSHOT_API_KEY"] = self.api_key_var.get().strip()
        # 统一 UTF-8：Windows 中文系统默认 GBK，遇 emoji/生僻字会乱码（pi-runtime.md §10）
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.base_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            if stdout:
                self._call_ui(self.append_status, stdout)
            if stderr:
                self._call_ui(self.append_status, stderr)
            if result.returncode != 0:
                self._call_ui(
                    self._show_error,
                    "执行失败",
                    f"命令执行失败，退出码: {result.returncode}",
                )
                return
            if on_success:
                self._call_ui(on_success)
        except Exception as exc:  # noqa: BLE001 - 后台线程异常必须回传界面
            self._call_ui(self._show_error, "异常", str(exc))

    def run_outline(self) -> None:
        api_key = self.api_key_var.get().strip()
        pdf_path = self.pdf_path_var.get().strip()
        if not api_key:
            messagebox.showwarning("缺少参数", "请先输入API密钥")
            return
        if not pdf_path:
            messagebox.showwarning("缺少参数", "请先输入PDF路径")
            return

        script = self.base_dir / "generate_outline.py"
        if not script.exists():
            messagebox.showerror("文件缺失", f"未找到脚本: {script}")
            return

        self.append_status("开始提取大纲...")
        model = self._current_model() or "kimi-k2-0905-preview"
        cmd = [sys.executable, str(script), pdf_path, "--model", model]

        def on_success() -> None:
            self.append_status("大纲提取完成")
            self.load_outline_to_editor()

        threading.Thread(target=self._run_subprocess, args=(cmd, on_success), daemon=True).start()

    def run_polish(self) -> None:
        api_key = self.api_key_var.get().strip()
        position = self.position_var.get().strip()
        text = self.input_text.get("1.0", "end").strip()
        if not api_key:
            messagebox.showwarning("缺少参数", "请先输入API密钥")
            return
        if not position:
            messagebox.showwarning("缺少参数", "请先输入润色位置")
            return
        if not text:
            messagebox.showwarning("缺少参数", "请先输入待润色文字")
            return

        script = self.base_dir / "polish.py"
        if not script.exists():
            messagebox.showerror("文件缺失", f"未找到脚本: {script}")
            return

        self.save_outline_from_editor()
        self.append_status("开始润色...")
        model = self._current_model() or "kimi-k2-0905-preview"
        cmd = [
            sys.executable,
            str(script),
            "--outline",
            str(self._outline_path()),
            "--position",
            position,
            "--text",
            text,
            "--output",
            str(self._output_path()),
            "--model",
            model,
        ]

        def on_success() -> None:
            out_file = self._output_path()
            if out_file.exists():
                try:
                    content = out_file.read_text(encoding="utf-8")
                except OSError:
                    content = ""
                self._set_text_content(self.output_text, content)
            self.append_status("润色完成")

        threading.Thread(target=self._run_subprocess, args=(cmd, on_success), daemon=True).start()

    # ------------------------------------------------------------------
    # Agent 运行时初始化 / 可用性探测
    # ------------------------------------------------------------------
    def _init_agent_runtime(self) -> None:
        """创建 PiRuntime 对象（不会启动 pi 子进程，start/spawn 发生在首次 ask）。"""
        if _agent_runtime is None:
            self._runtime_init_error = AGENT_IMPORT_ERROR or "未找到 agent_runtime.py"
            self._runtime = None
            return
        try:
            self._runtime = _agent_runtime.PiRuntime(
                base_dir=self.base_dir,
                on_event=self._on_agent_event,
            )
        except Exception as exc:  # noqa: BLE001
            self._runtime = None
            self._runtime_init_error = f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # Agent 模型：默认值 / 本地持久化 / 实时拉取可用模型
    # ------------------------------------------------------------------
    @staticmethod
    def _default_model() -> str:
        """默认模型：优先 agent_runtime 解析出的 THESIS_MODEL / 内置默认值。"""
        if _agent_runtime is not None:
            value = getattr(_agent_runtime, "MODEL", None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return os.environ.get("THESIS_MODEL", "").strip() or "kimi-k2-0905-preview"

    @staticmethod
    def _ui_settings_path() -> Path:
        return BASE_DIR / ".thesis" / "ui_settings.json"

    def _load_ui_settings(self) -> None:
        try:
            data = json.loads(self._ui_settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        model = str(data.get("model") or "").strip()
        if not model:
            return
        self.model_var.set(model)
        self._model_choices = [model]
        try:
            self.model_combo.configure(values=[model])
        except (AttributeError, tk.TclError):
            pass

    def _save_ui_settings(self) -> None:
        model = self._current_model()
        if not model:
            return
        try:
            path = self._ui_settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"model": model}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def _current_model(self) -> str:
        try:
            return self.model_var.get().strip()
        except tk.TclError:
            return ""

    def _on_model_selected(self, _event=None) -> None:
        self._save_ui_settings()
        model = self._current_model()
        if model:
            self.append_agent_log(
                f"模型已设为 {model}（下一次任务生效；切换会重启 pi 会话）", tag="muted"
            )

    def _set_refresh_models_state(self, state: str) -> None:
        try:
            self.btn_refresh_models.configure(state=state)
        except (AttributeError, tk.TclError):
            pass

    def refresh_models(self, *, auto: bool = False) -> None:
        """用当前 API Key 拉取账号可用模型列表并回填下拉框（后台线程，不阻塞 UI）。"""
        if self._models_loading:
            return
        if _agent_runtime is None:
            if not auto:
                self._show_error(
                    "Agent 运行时不可用", "未加载 agent_runtime.py，无法查询模型列表。"
                )
            return
        api_key = self.api_key_var.get().strip()
        if not api_key:
            if not auto:
                self._warn("缺少参数", "请先在顶部输入 kimi API 密钥，再刷新模型列表。")
                try:
                    self.api_key_entry.focus_set()
                except tk.TclError:
                    pass
            return
        self._models_loading = True
        self._set_refresh_models_state("disabled")
        if not auto:
            self.append_agent_log("正在获取当前 API Key 可用的模型列表…", tag="muted")

        def worker() -> None:
            try:
                models = _agent_runtime.fetch_moonshot_models(api_key)
            except Exception as exc:  # noqa: BLE001 - 统一在主线程日志里展示
                self._call_ui(self._on_models_failed, exc)
                return
            self._call_ui(self._on_models_loaded, models)

        threading.Thread(target=worker, name="thesis-models", daemon=True).start()

    def _on_models_loaded(self, models) -> None:
        self._models_loading = False
        self._set_refresh_models_state("disabled" if self._agent_busy else "normal")
        clean = [str(item).strip() for item in (models or []) if str(item).strip()]
        if not clean:
            return
        self._model_choices = clean
        current = self._current_model()
        chosen = current if current in clean else self._pick_preferred_model(clean)
        if chosen != current:
            self.model_var.set(chosen)
            self.append_agent_log(f"当前模型不可用，已自动选择：{chosen}", tag="ok")
        try:
            self.model_combo.configure(values=clean)
        except tk.TclError:
            pass
        shown = "、".join(clean[:8]) + ("…" if len(clean) > 8 else "")
        self.append_agent_log(f"已获取 {len(clean)} 个可用模型：{shown}", tag="muted")
        self._save_ui_settings()

    def _on_models_failed(self, exc: Exception) -> None:
        self._models_loading = False
        self._set_refresh_models_state("disabled" if self._agent_busy else "normal")
        self.append_agent_log(
            f"⚠️ 获取模型列表失败：{self._shorten(str(exc), 300)}", tag="error"
        )

    def _set_balance_button_state(self, state: str) -> None:
        try:
            self.btn_balance.configure(state=state)
        except (AttributeError, tk.TclError):
            pass

    def check_balance(self) -> None:
        """查询 Moonshot 账户余额/代金券（后台线程；用于诊断 429/quota）。"""
        if _agent_runtime is None:
            self._show_error("Agent 运行时不可用", "未加载 agent_runtime.py，无法查询余额。")
            return
        api_key = self.api_key_var.get().strip()
        if not api_key:
            self._warn("缺少参数", "请先在顶部输入 kimi API 密钥，再查询额度。")
            return
        if self._balance_loading:
            return
        self._balance_loading = True
        self._set_balance_button_state("disabled")
        self.append_agent_log("正在查询 Moonshot 账户额度…", tag="muted")

        def worker() -> None:
            try:
                data = _agent_runtime.fetch_moonshot_balance(api_key)
            except Exception as exc:  # noqa: BLE001
                self._call_ui(self._on_balance_failed, exc)
                return
            self._call_ui(self._on_balance_loaded, data)

        threading.Thread(target=worker, name="thesis-balance", daemon=True).start()

    def _on_balance_loaded(self, data) -> None:
        self._balance_loading = False
        self._set_balance_button_state("disabled" if self._agent_busy else "normal")

        def _fmt(value) -> str:
            return "—" if value is None else f"¥{float(value):.2f}"

        payload = data if isinstance(data, dict) else {}
        self.append_agent_log(
            "账户额度：可用 "
            f"{_fmt(payload.get('available_balance'))}（现金 {_fmt(payload.get('cash_balance'))}，"
            f"代金券 {_fmt(payload.get('voucher_balance'))}）",
            tag="ok",
        )

    def _on_balance_failed(self, exc: Exception) -> None:
        self._balance_loading = False
        self._set_balance_button_state("disabled" if self._agent_busy else "normal")
        self.append_agent_log(
            f"⚠️ 查询额度失败：{self._shorten(str(exc), 300)}", tag="error"
        )

    @staticmethod
    def _pick_preferred_model(models: list[str]) -> str:
        """在可用列表里挑一个较新的 Kimi 模型；找不到则取第一个。"""
        lowered = [(model, model.lower()) for model in models]
        for pattern in (
            "kimi-k3",
            "kimi-k2.7",
            "kimi-k2.6",
            "kimi-k2.5",
            "kimi-k2",
            "moonshot-v1-128k",
            "moonshot-v1-32k",
            "moonshot-v1-8k",
        ):
            for model, low in lowered:
                if pattern in low:
                    return model
        return models[0]

    @staticmethod
    def _looks_like_quota_error(error) -> bool:
        low = str(error or "").lower()
        return any(
            key in low for key in ("429", "quota", "rate limit", "rate_limit", "额度")
        )

    @staticmethod
    def _looks_like_model_error(error) -> bool:
        low = str(error or "").lower()
        return (
            "resource_not_found" in low
            or "not found the model" in low
            or "模型" in low and "不可用" in low
        )

    def _maybe_refresh_models_after_model_error(self, error) -> None:
        """模型 404 时自动拉取可用模型列表，并自动切换到一个可用模型。"""
        if not self._looks_like_model_error(error):
            return
        current = self._current_model()
        if current and current in self._model_choices:
            return  # 列表里明明有该模型，更可能是账号权限/额度问题，避免反复刷新
        if self._models_loading or not self.api_key_var.get().strip():
            return
        self.append_agent_log(
            "ℹ️ 该模型在当前 API Key 下不可用，正在自动获取可用模型列表…", tag="error"
        )
        self.refresh_models(auto=True)

    def _on_agent_event(self, event) -> None:
        """pi 读线程回调：只允许放入队列，绝不能触碰 tk 控件。"""
        try:
            self._agent_queue.put(event)
        except Exception:  # noqa: BLE001 - 读线程不能因回调异常而中断
            pass

    def _probe_agent_availability(self) -> None:
        self._probe_job = None
        if self._closing:
            return
        if _agent_runtime is None or self._runtime is None:
            reason = self._runtime_init_error or "未找到 agent_runtime.py"
            self._set_agent_available(False, reason, import_failed=True)
            return
        try:
            ok, reason = self._runtime.check_available()
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, f"{type(exc).__name__}: {exc}"
        self._set_agent_available(bool(ok), str(reason or ""))

    def _set_agent_available(
        self, available: bool, reason: str, *, import_failed: bool = False
    ) -> None:
        self._agent_ready = available
        self._agent_available_reason = reason
        if available:
            extra = ""
            prompt_path = self._system_prompt_path()
            if prompt_path is not None and not prompt_path.exists():
                extra = f"\n⚠️ 未找到系统提示词文件：{prompt_path}（任务可能在启动时失败）"
            self.header_badge.configure(text=f"{PI_BADGE} · ✅ 可用")
            self.agent_banner.configure(
                text=(
                    f"✅ pi 运行时可用：{reason or '就绪'}。可直接使用下方快捷操作，"
                    f"或在输入框中与 agent 多轮对话。{extra}"
                ),
                foreground="#166534",
            )
            self.append_agent_log(f"pi 运行时就绪：{reason or '就绪'}", tag="ok")
        else:
            if import_failed:
                detail = (
                    f"⚠️ 无法加载 agent_runtime 模块：{reason}\n"
                    "请确认项目根目录存在 agent_runtime.py；在此之前可继续使用"
                    "「传统流程（兼容）」标签页。"
                )
            else:
                detail = (
                    f"⚠️ pi 运行时不可用：{reason or '未知原因'}\n"
                    "仍可使用「传统流程（兼容）」标签页。安装/修复："
                    "npm i -g @earendil-works/pi-coding-agent（需要 Node.js，且 node 在 PATH 中），"
                    "或设置 THESIS_NODE / THESIS_PI_CLI 环境变量。"
                )
            self.header_badge.configure(text=f"{PI_BADGE} · ⚠️ 不可用")
            self.agent_banner.configure(text=detail, foreground="#b91c1c")
            self.append_agent_log(
                f"⚠️ Agent 运行时不可用：{reason or '未知原因'}", tag="error"
            )

    @staticmethod
    def _system_prompt_path() -> Path | None:
        if _agent_runtime is None:
            return None
        value = getattr(_agent_runtime, "SYSTEM_PROMPT_PATH", None)
        if value is None:
            return None
        try:
            return Path(value)
        except (TypeError, ValueError):
            return None

    def _log_system_prompt_once(self) -> None:
        if self._system_prompt_logged:
            return
        self._system_prompt_logged = True
        prompt_path = self._system_prompt_path()
        if prompt_path is None:
            return
        suffix = "" if prompt_path.exists() else "（⚠️ 文件不存在）"
        self.append_agent_log(f"系统提示词：{prompt_path}{suffix}", tag="muted")

    def _require_agent(self) -> bool:
        """确保 agent 运行时可用；不可用时给出中文指引并返回 False。"""
        if self._closing:
            return False
        if _agent_runtime is None or self._runtime is None:
            reason = self._runtime_init_error or "agent_runtime 模块不可用"
            self.append_agent_log(f"❌ 无法使用 Agent：{reason}", tag="error")
            self._show_error(
                "Agent 运行时不可用",
                f"无法加载 pi agent 运行时：\n{reason}\n\n{AGENT_GUIDANCE}",
            )
            return False
        if self._agent_ready is None:
            self._probe_agent_availability()
        if not self._agent_ready:
            # 用户可能刚装好 pi，点击时重新探测一次，无需重启 GUI
            try:
                ok, reason = self._runtime.check_available()
            except Exception as exc:  # noqa: BLE001
                ok, reason = False, f"{type(exc).__name__}: {exc}"
            self._set_agent_available(bool(ok), str(reason or ""))
            if not ok:
                self._show_error(
                    "pi 运行时不可用",
                    f"检测失败：{reason or '未知原因'}\n\n{AGENT_GUIDANCE}",
                )
                return False
        return True

    # ------------------------------------------------------------------
    # Agent 标签页的日志 / 流式输出
    # ------------------------------------------------------------------
    def append_agent_log(self, line: str, tag: str | None = None) -> None:
        """向状态日志追加一行（只读控件，自动滚动到底部）。"""
        if self._closing:
            return
        try:
            self.agent_log_text.configure(state="normal")
            self.agent_log_text.insert("end", line.rstrip("\n") + "\n", (tag,) if tag else ())
            self.agent_log_text.see("end")
            self.agent_log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _append_agent_stream(self, text: str) -> None:
        if not text or self._closing:
            return
        try:
            self.agent_stream_text.configure(state="normal")
            self.agent_stream_text.insert("end", text)
            self.agent_stream_text.see("end")
            self.agent_stream_text.configure(state="disabled")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------
    # Agent 事件轮询（主线程）
    # ------------------------------------------------------------------
    def _poll_agent_events(self) -> None:
        """每 80ms 把读线程队列里的事件搬到界面上。"""
        if self._closing:
            return
        self._poll_job = None
        try:
            for _ in range(MAX_EVENTS_PER_POLL):
                try:
                    event = self._agent_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_agent_event(event)
                except Exception as exc:  # noqa: BLE001 - 单个坏事件不能打断轮询
                    self.append_agent_log(
                        f"⚠️ 处理 agent 事件失败：{type(exc).__name__}: {exc}", tag="error"
                    )
            self._sync_busy_with_runtime()
        finally:
            if not self._closing:
                try:
                    self._poll_job = self.root.after(POLL_INTERVAL_MS, self._poll_agent_events)
                except tk.TclError:
                    self._poll_job = None

    def _sync_busy_with_runtime(self) -> None:
        """兜底：若运行时已空闲但没收到 done 事件，恢复界面避免永久锁死。"""
        if not self._agent_busy or self._runtime is None:
            self._idle_since = None
            return
        try:
            still_busy = bool(getattr(self._runtime, "busy", False))
        except Exception:  # noqa: BLE001
            still_busy = False
        if still_busy:
            self._idle_since = None
            return
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
            return
        if now - self._idle_since >= 3.0:
            self._idle_since = None
            self._pending_action = None
            self._set_agent_busy(False)
            self.append_agent_log(
                "ℹ️ 运行时已空闲（未收到 done 事件），已恢复操作按钮。", tag="muted"
            )

    def _handle_agent_event(self, event) -> None:
        kind = str(getattr(event, "kind", "") or "").strip()
        text = getattr(event, "text", "") or ""
        data = getattr(event, "data", None)
        if not isinstance(data, dict):
            data = {}

        if kind == "text_delta":
            if text:
                self._stream_buffer += str(text)
                self._append_agent_stream(str(text))
        elif kind == "tool_start":
            name = self._event_tool_name(data, text)
            args = data.get("args", data.get("arguments"))
            self.append_agent_log(f"🔧 {name}({self._format_tool_args(args)})", tag="tool")
        elif kind == "tool_update":
            name = self._event_tool_name(data, text)
            progress = self._event_short_text(
                data, text, ("text", "message", "progress", "partial", "partialResult")
            )
            if progress and self._tool_update_seen.get(name) != progress:
                self._tool_update_seen[name] = progress
                self.append_agent_log(f"   … {name}: {progress}", tag="muted")
        elif kind == "tool_end":
            name = self._event_tool_name(data, text)
            is_error = bool(data.get("is_error") or data.get("isError") or data.get("error"))
            # 优先展示 data 里的工具结果摘要（result_text/summary），否则退回 text
            summary = self._event_short_text(
                data,
                text,
                ("summary", "result_text", "result", "text", "error", "message"),
                text_first=False,
            )
            if not summary:
                summary = "工具执行出错" if is_error else "完成"
            icon = "❌" if is_error else "✅"
            self.append_agent_log(
                f"{icon} {name} → {summary}", tag="error" if is_error else "ok"
            )
        elif kind == "status":
            message = self._event_short_text(data, text, ("text", "message", "status", "phase"))
            if message:
                self.append_agent_log(f"ℹ️ {message}", tag="muted")
        elif kind == "error":
            error = self._event_short_text(data, text, ("text", "message", "error")) or "未知错误"
            self._last_agent_error = error
            self.append_agent_log(f"❌ {error}", tag="error")
        elif kind == "session":
            session_id = (
                data.get("id") or data.get("sessionId") or data.get("session_id") or ""
            )
            suffix = f"：{session_id}" if session_id else ""
            self.append_agent_log(f"🆕 会话已建立{suffix}", tag="muted")
        elif kind == "done":
            self._handle_agent_done(data)
        else:
            if text:
                self.append_agent_log(
                    f"[{kind or 'event'}] {self._shorten(str(text), 300)}", tag="muted"
                )

    def _handle_agent_done(self, data: dict) -> None:
        ok = bool(data.get("ok", True))
        aborted = bool(data.get("aborted", False))
        error = data.get("error") or self._last_agent_error
        usage = data.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        action = self._pending_action
        self._pending_action = None
        self._idle_since = None
        self._set_agent_busy(False)

        # 最终助手文本：优先 done.data["text"]，否则读 runtime.last_assistant_text
        final_text = data.get("text")
        if not isinstance(final_text, str) or not final_text.strip():
            final_text = self._runtime_last_text()
        self._append_final_assistant_text(final_text)

        if ok:
            self.append_agent_log(f"✅ 任务完成{self._usage_note(usage)}", tag="ok")
            # 按发起时的动作类型自动载入结果文件（写盘由 agent 的 thesis_* 工具完成）
            if action == "outline":
                self._auto_load_outline()
            elif action == "polish":
                self._auto_load_output()
        elif aborted:
            suffix = "（未自动载入结果文件）" if action in ("outline", "polish") else ""
            self.append_agent_log(
                f"⏹ 任务已停止{self._usage_note(usage)}{suffix}。", tag="muted"
            )
        else:
            message = self._shorten(str(error or "未知错误"), 300)
            if action is None:
                # ask() 同步抛错时 runtime 仍会补发 error/done 事件；异常已弹窗提示，
                # 这里只记一行，避免重复的「任务失败」诊断。
                self.append_agent_log(f"ℹ️ 任务未完成：{message}", tag="muted")
            else:
                self.append_agent_log(f"❌ 任务失败：{message}", tag="error")
                self.append_agent_log(
                    "（未自动载入结果文件；可重试，或切换到「传统流程（兼容）」标签页。）",
                    tag="muted",
                )
            # 模型 404 / 无权限时，自动拉取该 API Key 的可用模型列表并切换
            self._maybe_refresh_models_after_model_error(error)
            # 429 等失败：若本轮结果文件已成功落盘，仍自动载入并提示「实际已完成」
            self._recover_saved_artifact_after_failure(action, str(error or ""))
            # 429 / 额度问题：给出额度/等待/换模型的可操作建议
            if self._looks_like_quota_error(error):
                self._last_quota_error_at = time.time()
                self.append_agent_log(
                    "建议：点顶部「查询额度」确认余额/代金券；**重试请求同样计入 RPM**，"
                    "请等待 ≥60 秒再发起新任务；或改用「传统流程（兼容）」的单次调用流程。",
                    tag="muted",
                )
        self._stream_buffer = ""
        self._tool_update_seen = {}
        self._action_started_at = None

    def _append_final_assistant_text(self, final_text: str) -> None:
        """done 时确保最终回复完整可见（流式内容缺失时补全）。"""
        if not final_text or not final_text.strip():
            return
        final = final_text.strip()
        streamed = self._stream_buffer.strip()
        if not streamed:
            self._append_agent_stream(final)
            return
        if final == streamed or final in streamed:
            return  # 流式已完整显示，避免重复
        self._append_agent_stream("\n\n—— 最终回复 ——\n" + final)

    def _runtime_last_text(self) -> str:
        if self._runtime is None:
            return ""
        try:
            value = getattr(self._runtime, "last_assistant_text", "") or ""
        except Exception:  # noqa: BLE001
            return ""
        return str(value)

    # ------------------------------------------------------------------
    # Agent 事件字段的容错解析（不同实现可能把信息放在 text 或 data 里）
    # ------------------------------------------------------------------
    @staticmethod
    def _event_tool_name(data: dict, text: str) -> str:
        name = data.get("toolName") or data.get("tool_name") or data.get("name") or ""
        if not name and text:
            name = str(text).strip().split("(", 1)[0].split(" ", 1)[0].strip()
        return str(name) or "tool"

    @classmethod
    def _event_short_text(
        cls, data: dict, text: str, keys: tuple[str, ...], *, text_first: bool = True
    ) -> str:
        """从事件 text / data 里取一段可读文本。

        text_first=False 时优先用 data 中命中的字段（例如 tool_end.result_text），
        取不到再回退到事件 text。
        """
        if not text_first:
            for key in keys:
                value = data.get(key)
                if value in (None, "", [], {}):
                    continue
                return cls._shorten(cls._value_to_text(value), 300)
        if text and str(text).strip():
            return cls._shorten(str(text), 300)
        for key in keys:
            value = data.get(key)
            if value in (None, "", [], {}):
                continue
            return cls._shorten(cls._value_to_text(value), 300)
        return ""

    @staticmethod
    def _value_to_text(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
        text = " ".join(text.split())
        if len(text) > limit:
            text = text[: max(0, limit - 1)] + "…"
        return text

    @classmethod
    def _format_tool_args(cls, args) -> str:
        if args is None or args == {} or args == "":
            return ""
        if isinstance(args, str):
            return cls._shorten(args, 160)
        try:
            raw = json.dumps(args, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(args)
        return cls._shorten(raw, 160)

    @staticmethod
    def _usage_note(usage: dict) -> str:
        for key in ("total_tokens", "totalTokens", "total", "output_tokens", "outputTokens"):
            value = usage.get(key)
            if isinstance(value, int) and value > 0:
                return f"（tokens：{value}）"
        # 部分 pi 版本用 input/output 表示 token 用量
        input_tokens = usage.get("inputTokens", usage.get("input_tokens", usage.get("input")))
        output_tokens = usage.get("outputTokens", usage.get("output_tokens", usage.get("output")))
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            total = input_tokens + output_tokens
            if total > 0:
                return f"（tokens：{total}）"
        return ""

    # ------------------------------------------------------------------
    # Agent 动作：开始 / 提交 / 失败 / 停止 / 新对话 / 自动载入
    # ------------------------------------------------------------------
    def _set_agent_busy(self, busy: bool) -> None:
        self._agent_busy = busy
        state = "disabled" if busy else "normal"
        for button in getattr(self, "_agent_action_buttons", ()):
            try:
                button.configure(state=state)
            except tk.TclError:
                pass
        try:
            self.btn_agent_stop.configure(state="normal" if busy else "disabled")
        except tk.TclError:
            pass
        self._set_refresh_models_state(
            "disabled" if (busy or self._models_loading) else "normal"
        )
        self._set_balance_button_state(
            "disabled" if (busy or self._balance_loading) else "normal"
        )

    def _begin_agent_action(
        self,
        kind: str,
        *,
        clear_stream: bool = True,
        clear_log: bool = False,
        retry_call=None,
    ) -> str | None:
        """校验密钥/运行时，清空上一轮输出并进入 busy 状态；失败返回 None。

        ``retry_call``：给定时若距上次 429 不足 60 秒，会自动等待剩余冷却时间后
        再调用一次（用于「Agent 提取大纲 / 润色」这类快捷任务，避免重试风暴）。
        """
        if self._agent_busy:
            self._warn("请稍候", "Agent 正在处理中，请先等待完成或点击「停止」。")
            return None
        if self._cooldown_waiting:
            self._warn("请稍候", "正在等待限流冷却（60 秒窗口），稍后会自动开始。")
            return None
        api_key = self.api_key_var.get().strip()
        if not api_key:
            self._warn("缺少参数", "请先在顶部输入 kimi API 密钥。")
            try:
                self.api_key_entry.focus_set()
            except tk.TclError:
                pass
            return None
        if not self._require_agent():
            return None
        try:
            runtime_busy = bool(getattr(self._runtime, "busy", False))
        except Exception:  # noqa: BLE001
            runtime_busy = False
        if runtime_busy:
            self._warn("请稍候", "pi 运行时仍在处理上一条请求，请稍后重试。")
            return None

        self._pending_action = kind
        self._action_started_at = time.time()
        self._stream_buffer = ""
        self._last_agent_error = ""
        self._tool_update_seen = {}
        self._idle_since = None
        if clear_log:
            # 快捷流程每次任务使用干净的状态日志；对话则保留历史
            self._set_text_content(self.agent_log_text, "", readonly=True)
        if clear_stream:
            self._set_text_content(self.agent_stream_text, "", readonly=True)
        elif self._get_text_content(self.agent_stream_text).strip():
            # 多轮对话：不覆盖上一轮内容，用分隔线隔开
            self._append_agent_stream("\n\n——— 新消息 ———\n")
        # 冷却处理：3 RPM 账号在上次 429 后 60 秒内再次发起任务几乎必然再次限流，
        # 且重试请求同样占用 RPM 配额，因此对快捷任务自动等待剩余冷却时间。
        last_quota = self._last_quota_error_at
        if last_quota is not None and (time.time() - last_quota) < 60:
            wait = max(1, int(60 - (time.time() - last_quota)))
            if retry_call is not None:
                self.append_agent_log(
                    f"⏳ 距上次 429 限流不足 60 秒（重试请求同样占用 RPM），"
                    f"已安排 {wait} 秒冷却后自动开始…",
                    tag="error",
                )
                self._cooldown_waiting = True

                def _resume() -> None:
                    self._cooldown_job = None
                    self._cooldown_waiting = False
                    if not self._closing:
                        retry_call()

                try:
                    self._cooldown_job = self.root.after(wait * 1000, _resume)
                except tk.TclError:
                    self._cooldown_waiting = False
                return None
            self.append_agent_log(
                f"⚠️ 距上次 429 限流不足 60 秒（建议再等约 {wait} 秒）。"
                "重试请求同样占用 RPM 配额，过早重试可能持续被限流；"
                "也可改用「传统流程（兼容）」标签页的单次调用。",
                tag="error",
            )
        self._set_agent_busy(True)
        self._log_system_prompt_once()
        return api_key

    @staticmethod
    def _describe_exception(exc: Exception) -> str:
        text = str(exc).strip() or type(exc).__name__
        if _agent_runtime is not None:
            for name in ("PiUnavailable", "PiRuntimeError"):
                exc_type = getattr(_agent_runtime, name, None)
                if isinstance(exc_type, type) and isinstance(exc, exc_type):
                    return f"[{name}] {text}"
        return f"[{type(exc).__name__}] {text}"

    def _submit_agent_prompt(self, prompt: str, api_key: str, label: str) -> bool:
        model = self._current_model()
        try:
            self._runtime.ask(prompt, api_key=api_key, model=model or None)
        except Exception as exc:  # noqa: BLE001 - PiUnavailable / PiRuntimeError 等
            self._fail_agent_action(
                f"{label}启动失败：{self._describe_exception(exc)}", guidance=True
            )
            return False
        if model:
            self._save_ui_settings()
        self.append_agent_log(f"模型：{model or '（默认）'}", tag="muted")
        self.append_agent_log(f"已提交「{label}」请求，等待 pi 流式响应…", tag="muted")
        return True

    def _fail_agent_action(
        self, message: str, *, show_dialog: bool = True, guidance: bool = False
    ) -> None:
        self._pending_action = None
        self._idle_since = None
        self._set_agent_busy(False)
        self.append_agent_log(f"❌ {message}", tag="error")
        if show_dialog:
            text = f"{message}\n\n{AGENT_GUIDANCE}" if guidance else message
            self._show_error("Agent 执行失败", text)

    def agent_run_outline(self) -> None:
        """快捷操作：让 pi agent 通过工具解析 PDF 并落盘 outline.txt。"""
        pdf_raw = self.pdf_path_var.get().strip()
        if not pdf_raw:
            self._warn("缺少参数", "请先在顶部输入或选择 PDF 路径。")
            return
        pdf_path = self._resolve_path(pdf_raw, "test2.pdf")
        if not pdf_path.exists():
            self._show_error("文件不存在", f"未找到 PDF 文件：\n{pdf_path}")
            return
        api_key = self._begin_agent_action(
            "outline", clear_stream=True, clear_log=True, retry_call=self.agent_run_outline
        )
        if not api_key:
            return
        self.append_agent_log("=" * 60, tag="muted")
        self.append_agent_log("▶ 任务：Agent 提取论文大纲", tag="tool")
        self.append_agent_log(f"PDF：{pdf_path}", tag="muted")
        try:
            prompt = _agent_runtime.build_outline_prompt(pdf_path)
        except Exception as exc:  # noqa: BLE001
            self._fail_agent_action(f"构造大纲提示词失败：{self._describe_exception(exc)}")
            return
        self._submit_agent_prompt(prompt, api_key, "提取大纲")

    def agent_run_polish(self) -> None:
        """快捷操作：保存当前大纲后，让 pi agent 润色「待润色文字」。"""
        api_key = self.api_key_var.get().strip()
        position = self.position_var.get().strip()
        text = self.input_text.get("1.0", "end").strip()
        if not api_key:
            self._warn("缺少参数", "请先在顶部输入 kimi API 密钥。")
            return
        if not position:
            self._warn("缺少参数", "请先在顶部输入润色位置（例如：第三章 3.2）。")
            return
        if not text:
            self._warn(
                "缺少参数",
                "请先在「传统流程（兼容）」标签页的「待润色文字」中粘贴需要润色的文本。",
            )
            return
        # 先保存编辑器里的大纲，保证 agent 读到的 outline.txt 是最新版本
        if not self.save_outline_from_editor():
            return
        outline_path = self._outline_path()
        try:
            outline_empty = not outline_path.read_text(encoding="utf-8").strip()
        except OSError:
            outline_empty = True
        if outline_empty:
            self._warn(
                "大纲为空",
                "大纲文件为空，请先提取/填写大纲后再润色。\n"
                "（可直接使用「Agent 提取大纲」或「传统流程」的「提取论文大纲」。）",
            )
            return
        api_key = self._begin_agent_action(
            "polish", clear_stream=True, clear_log=True, retry_call=self.agent_run_polish
        )
        if not api_key:
            return
        self.append_agent_log("=" * 60, tag="muted")
        self.append_agent_log("▶ 任务：Agent 润色当前段落", tag="tool")
        self.append_agent_log(f"位置：{position}", tag="muted")
        self.append_agent_log(f"大纲：{outline_path}", tag="muted")
        try:
            prompt = _agent_runtime.build_polish_prompt(position, text, outline_path)
        except Exception as exc:  # noqa: BLE001
            self._fail_agent_action(f"构造润色提示词失败：{self._describe_exception(exc)}")
            return
        self._submit_agent_prompt(prompt, api_key, "润色段落")

    def agent_send(self) -> None:
        """发送输入框中的自由对话/指令。"""
        text = self.agent_input_text.get("1.0", "end").strip()
        if not text:
            self._warn("缺少参数", "请先输入要发送给 agent 的内容。")
            try:
                self.agent_input_text.focus_set()
            except tk.TclError:
                pass
            return
        api_key = self._begin_agent_action("chat", clear_stream=False)
        if not api_key:
            return
        self.append_agent_log("=" * 60, tag="muted")
        self.append_agent_log("▶ 任务：与 agent 对话", tag="tool")
        self.append_agent_log(f"用户：{self._shorten(text, 160)}", tag="muted")
        try:
            prompt = _agent_runtime.build_chat_prompt(text)
        except Exception as exc:  # noqa: BLE001
            self._fail_agent_action(f"构造对话提示词失败：{self._describe_exception(exc)}")
            return
        if self._submit_agent_prompt(prompt, api_key, "对话"):
            self.agent_input_text.delete("1.0", "end")

    def _on_agent_input_ctrl_enter(self, _event: tk.Event) -> str:
        self.agent_send()
        return "break"

    def agent_stop(self) -> None:
        """请求中止当前任务（abort 响应是异步的，等 done 事件恢复界面）。"""
        if _agent_runtime is None or self._runtime is None:
            self.append_agent_log("ℹ️ Agent 运行时不可用，无需停止。", tag="muted")
            return
        if not self._agent_busy:
            self.append_agent_log("ℹ️ 当前没有正在执行的任务。", tag="muted")
            return
        try:
            self._runtime.abort()
        except Exception as exc:  # noqa: BLE001
            self.append_agent_log(
                f"⚠️ 停止请求失败：{self._describe_exception(exc)}", tag="error"
            )
            return
        self.append_agent_log("⏹ 已请求停止当前任务，等待 pi 确认…", tag="muted")
        if not self._closing:
            try:
                self.root.after(15000, self._abort_watchdog)
            except tk.TclError:
                pass

    def _abort_watchdog(self) -> None:
        """abort 15s 仍未收到 done 时强制结束后台 pi 进程，并恢复界面。"""
        if self._closing or not self._agent_busy:
            return
        self._pending_action = None
        self._idle_since = None
        self._action_started_at = None
        self._set_agent_busy(False)
        self.append_agent_log(
            "⚠️ 停止确认超时（15s）：pi 可能仍在自动重试 429。已恢复操作按钮并尝试强制结束后台进程…",
            tag="error",
        )
        runtime = self._runtime
        if runtime is not None:
            try:
                runtime.stop()
                self.append_agent_log(
                    "🛑 已强制结束 pi 运行时；下一次任务会自动重新启动（pi 会话上下文清空）。",
                    tag="muted",
                )
            except Exception as exc:  # noqa: BLE001 - 关闭失败不应影响 UI
                self.append_agent_log(
                    f"⚠️ 强制结束 pi 运行时失败：{type(exc).__name__}: {exc}", tag="error"
                )

    def agent_new_session(self) -> None:
        """新建 pi 会话并清空两个输出面板。"""
        if self._agent_busy:
            self._warn("请稍候", "Agent 正在处理中，请先等待完成或点击「停止」。")
            return
        if not self._require_agent():
            return
        try:
            self._runtime.new_session()
        except Exception as exc:  # noqa: BLE001
            message = f"新建会话失败：{self._describe_exception(exc)}"
            self.append_agent_log(f"❌ {message}", tag="error")
            self._show_error("新建会话失败", f"{message}\n\n{AGENT_GUIDANCE}")
            return
        self._pending_action = None
        self._stream_buffer = ""
        self._last_agent_error = ""
        self._tool_update_seen = {}
        self._set_text_content(self.agent_log_text, "", readonly=True)
        self._set_text_content(self.agent_stream_text, "", readonly=True)
        self.append_agent_log("🆕 已新建对话（清空上下文），等待输入。", tag="ok")

    def _recover_saved_artifact_after_failure(self, action: str | None, error_text: str) -> None:
        """任务最终失败时，若本轮结果文件其实已成功落盘，则自动载入并提示「实际已完成」。

        典型场景：agent 已完成 thesis_outline_save，但最后一步（汇报/收尾）模型调用
        被 429 限流；此时不应让用户误以为结果丢了。
        """
        if action not in ("outline", "polish"):
            return
        artifact = self._outline_path() if action == "outline" else self._output_path()
        started = self._action_started_at
        if started is None:
            return
        try:
            fresh = artifact.is_file() and artifact.stat().st_mtime >= (started - 1.0)
        except OSError:
            fresh = False
        if not fresh:
            return
        if action == "outline":
            self._auto_load_outline()
        else:
            self._auto_load_output()
        self.append_agent_log(
            "✅ 本轮结果文件已成功落盘：虽然最后一步（汇报/收尾）失败，任务实际已完成。",
            tag="ok",
        )
        self.append_agent_log(
            "提示：若账号组织 RPM 很低（例如 3 RPM），可改用「传统流程（兼容）」标签页的单次调用流程，"
            "或到 Moonshot 控制台申请提高 RPM。",
            tag="muted",
        )

    def _auto_load_outline(self) -> None:
        """Agent 大纲任务完成后自动把 outline.txt 载入大纲编辑器。"""
        path = self._outline_path()
        try:
            if not path.exists():
                self.append_agent_log(
                    f"⚠️ 未找到大纲文件：{path}（agent 可能没有调用 thesis_outline_save）",
                    tag="error",
                )
                return
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.append_agent_log(f"⚠️ 读取大纲文件失败：{exc}", tag="error")
            return
        self._set_text_content(self.outline_text, content)
        self.append_status(f"已载入大纲: {path}")
        self.append_agent_log(f"📥 已自动载入大纲到编辑器：{path}", tag="ok")

    def _auto_load_output(self) -> None:
        """Agent 润色任务完成后自动把 polished.txt 载入润色输出面板。"""
        path = self._output_path()
        try:
            if not path.exists():
                self.append_agent_log(
                    f"⚠️ 未找到润色输出文件：{path}（agent 可能没有调用 thesis_save_polished）",
                    tag="error",
                )
                return
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.append_agent_log(f"⚠️ 读取润色输出失败：{exc}", tag="error")
            return
        self._set_text_content(self.output_text, content)
        self.append_status(f"已载入润色输出: {path}")
        self.append_agent_log(f"📥 已自动载入润色结果到输出面板：{path}", tag="ok")

    # ------------------------------------------------------------------
    # 关闭窗口
    # ------------------------------------------------------------------
    def on_close(self) -> None:
        if self._closing:
            return
        self._closing = True

        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except (tk.TclError, ValueError):
                pass
            self._poll_job = None

        if self._ui_poll_job is not None:
            try:
                self.root.after_cancel(self._ui_poll_job)
            except (tk.TclError, ValueError):
                pass
            self._ui_poll_job = None

        if self._probe_job is not None:
            try:
                self.root.after_cancel(self._probe_job)
            except (tk.TclError, ValueError):
                pass
            self._probe_job = None

        if self._cooldown_job is not None:
            try:
                self.root.after_cancel(self._cooldown_job)
            except (tk.TclError, ValueError):
                pass
            self._cooldown_job = None
        self._cooldown_waiting = False

        runtime = self._runtime
        if runtime is not None:
            # pi-runtime.md §8.1/§10.4：由 runtime.stop() 内部负责 abort + 终止进程 + join 线程，
            # 这里只做异常保护，绝不信任 Windows 上的退出码。
            try:
                runtime.stop()
            except Exception:  # noqa: BLE001 - 关闭窗口时任何异常都不能阻塞退出
                pass

        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main() -> None:
    root = tk.Tk()
    app = ThesisGUI(root)
    try:
        root.mainloop()
    except tk.TclError:
        # 窗口已被销毁（例如 WM_DELETE_WINDOW 回调里 destroy）时正常退出
        pass
    finally:
        # 兜底：无论以何种方式退出主循环，都确保 pi 子进程被回收
        try:
            app.on_close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
