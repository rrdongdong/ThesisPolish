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
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import ui_theme

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
PI_BADGE = "pi 0.85.1 · Kimi · v3.0(2026-10-27)"

# 状态日志标签配色（值 = (前景色 key, 背景色 key)）
LOG_TAG_COLORS = {
    "error": ("error", None),
    "ok": ("ok", None),
    "tool": ("accent", None),
    "muted": ("text_muted", None),
}

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
        self._doctor_running = False  # 是否正在运行限流诊断
        self._last_raw_error_logged = ""  # 避免重复打印同一条原始错误
        # 实时预览：agent 正在生成的工具参数（thesis_outline_save / thesis_save_polished）
        self._live_tool: dict | None = None
        self._live_args = ""
        self._live_last_update = 0.0
        # 实时文件监听：agent / 传统流程写 outline.txt 时立即载入编辑器
        self._outline_watch_job: str | None = None
        self._outline_watch_mtime: float | None = None
        self._legacy_outline_running = False
        self._banner_wrap = 0
        self._banner_state = "info"

        # --- 现代 UI 主题（深色 / 浅色，启动前读取上次选择） ---
        ui_theme.enable_dpi_awareness()
        self._theme_name = self._read_theme_name()
        self.theme = ui_theme.Theme(root, self._theme_name)
        ui_theme.configure_ttk(ttk.Style(root), self.theme)

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
        self.api_key_var.trace_add("write", lambda *_args: self._on_api_key_changed())
        self._update_conn_label()
        self._poll_job = self.root.after(POLL_INTERVAL_MS, self._poll_agent_events)
        # 工作线程 → 主线程的通用回调队列（refresh_models 等；不直接跨线程调用 Tk）
        self._ui_poll_job = self.root.after(60, self._poll_ui_calls)
        # 启动后再探测 pi 可用性，让窗口先显示出来（不阻塞 GUI 构建）
        self._probe_job = self.root.after(120, self._probe_agent_availability)

    # ------------------------------------------------------------------
    # UI 构建
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        p = self.theme.palette
        self.root.title("ThesisPolish · 论文大纲提取与段落润色")
        self.root.configure(bg=p["bg"])
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        try:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            width = min(1280, max(1080, screen_w - 96))
            height = min(900, max(720, screen_h - 140))
            self.root.geometry(f"{width}x{height}")
            self.root.minsize(1040, 700)
        except tk.TclError:
            pass
        self._build_header_bar()
        self._build_body()
        self._build_statusbar()
        self._apply_theme(self._theme_name, save=False, rebuild_styles=True)

    # -- 顶栏 -----------------------------------------------------------
    def _build_header_bar(self) -> None:
        p = self.theme.palette
        self.header = ui_theme.ThemedPanel(self.root, self.theme, bg_key="surface", height=64)
        self.header.grid(row=0, column=0, sticky="we")
        self.header.grid_propagate(False)
        self.header.columnconfigure(2, weight=1)
        ui_theme.Brand(self.header, self.theme).grid(row=0, column=0, padx=(18, 10), pady=14)
        titles = tk.Frame(self.header, bd=0, highlightthickness=0, bg=p["surface"])
        titles.grid(row=0, column=1, sticky="w")
        ui_theme.ThemedLabel(
            titles, self.theme, "ThesisPolish", fg_key="text", bg_key="surface",
            size=14, weight="bold",
        ).pack(anchor="w")
        ui_theme.ThemedLabel(
            titles, self.theme, "论文大纲提取 · 段落润色 · pi agent 驱动",
            fg_key="text_muted", bg_key="surface", size=9,
        ).pack(anchor="w")
        right = tk.Frame(self.header, bd=0, highlightthickness=0, bg=p["surface"])
        right.grid(row=0, column=3, sticky="e", padx=(10, 18))
        self.status_pill = ui_theme.Pill(
            right, self.theme, "空闲", fg_key="text_muted", bg_key="surface_alt",
            surface_key="surface",
        )
        self.status_pill.pack(side="left", padx=(0, 10), pady=6)
        self.btn_theme = ttk.Button(
            right, text="◐ 浅色", style="Ghost.TButton", command=self._toggle_theme
        )
        self.btn_theme.pack(side="left")
        ui_theme.Divider(self.root, self.theme).grid(row=0, column=0, sticky="sew")

    # -- 主体：侧边栏 + 主内容 -------------------------------------------
    def _build_body(self) -> None:
        p = self.theme.palette
        body = tk.Frame(self.root, bd=0, highlightthickness=0, bg=p["bg"])
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(2, weight=1)
        body.rowconfigure(0, weight=1)
        self.sidebar = ui_theme.ThemedPanel(body, self.theme, bg_key="surface_alt", width=356)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.grid_propagate(False)
        self.sidebar.columnconfigure(0, weight=1)
        ui_theme.Divider(body, self.theme).grid(row=0, column=1, sticky="ns")
        self.main = tk.Frame(body, bd=0, highlightthickness=0, bg=p["bg"])
        self.main.grid(row=0, column=2, sticky="nsew")
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(1, weight=1)
        self._build_sidebar(self.sidebar)
        self._build_main(self.main)

    def _card(self, parent, title=None, hint=None, padding=(14, 12)):
        card = ui_theme.Card(parent, self.theme, title=title, hint=hint, padding=padding)
        return card, card.body

    def _build_sidebar(self, sidebar: tk.Widget) -> None:
        p = self.theme.palette
        card, body = self._card(sidebar, "运行配置", "API / 模型")
        card.grid(row=0, column=0, sticky="we", padx=14, pady=(14, 10))
        body.columnconfigure(0, weight=1)
        ui_theme.ThemedLabel(
            body, self.theme, "API 密钥", fg_key="text_muted", bg_key="card", size=9
        ).grid(row=0, column=0, sticky="w")
        self.api_key_entry = ttk.Entry(
            body, textvariable=self.api_key_var, show="*", style="Card.TEntry"
        )
        self.api_key_entry.grid(row=1, column=0, sticky="we", pady=(4, 0))
        self.btn_api_toggle = ttk.Button(
            body, text="显示", style="Small.TButton", command=self._toggle_api_visibility
        )
        self.btn_api_toggle.grid(row=1, column=1, sticky="e", padx=(6, 0), pady=(4, 0))
        ui_theme.ThemedLabel(
            body, self.theme, "模型", fg_key="text_muted", bg_key="card", size=9
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(12, 0))
        model_row = tk.Frame(body, bd=0, highlightthickness=0, bg=p["card"])
        model_row.grid(row=3, column=0, columnspan=2, sticky="we", pady=(4, 0))
        model_row.columnconfigure(0, weight=1)
        self.model_combo = ttk.Combobox(
            model_row, textvariable=self.model_var, values=(self.model_var.get(),)
        )
        self.model_combo.grid(row=0, column=0, sticky="we")
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)
        self.btn_refresh_models = ttk.Button(
            model_row, text="刷新", style="Small.TButton", command=self.refresh_models
        )
        self.btn_refresh_models.grid(row=0, column=1, sticky="e", padx=(6, 0))
        actions = tk.Frame(body, bd=0, highlightthickness=0, bg=p["card"])
        actions.grid(row=4, column=0, columnspan=2, sticky="we", pady=(10, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.btn_balance = ttk.Button(
            actions, text="查询额度", style="Small.TButton", command=self.check_balance
        )
        self.btn_balance.grid(row=0, column=0, sticky="we", padx=(0, 4))
        self.btn_doctor = ttk.Button(
            actions, text="诊断限流", style="Small.TButton", command=self.run_doctor
        )
        self.btn_doctor.grid(row=0, column=1, sticky="we", padx=(4, 0))
        self.balance_label = ui_theme.ThemedLabel(
            body, self.theme, "额度：点击「查询额度」查看", fg_key="text_faint", bg_key="card", size=9
        )
        self.balance_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        work, wbody = self._card(sidebar, "工作区", "路径")
        work.grid(row=1, column=0, sticky="we", padx=14, pady=(0, 10))
        wbody.columnconfigure(0, weight=1)
        row = 0
        for label_text, variable in (
            ("PDF 路径", self.pdf_path_var),
            ("大纲文件", self.outline_path_var),
            ("润色输出", self.output_path_var),
            ("润色位置", self.position_var),
        ):
            ui_theme.ThemedLabel(
                wbody, self.theme, label_text, fg_key="text_muted", bg_key="card", size=9
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0 if row == 0 else 10, 0))
            ttk.Entry(wbody, textvariable=variable, style="Card.TEntry").grid(
                row=row + 1, column=0, sticky="we", pady=(4, 0)
            )
            if label_text == "PDF 路径":
                self.btn_pick_pdf = ttk.Button(
                    wbody, text="选择", style="Small.TButton", command=self.pick_pdf
                )
                self.btn_pick_pdf.grid(row=row + 1, column=1, sticky="e", padx=(6, 0), pady=(4, 0))
            row += 2

        sidebar.rowconfigure(2, weight=1)
        footer = tk.Frame(sidebar, bd=0, highlightthickness=0, bg=p["surface_alt"])
        footer.grid(row=3, column=0, sticky="we", padx=16, pady=(0, 12))
        self.header_badge = ttk.Label(footer, text=PI_BADGE, style="SidebarMuted.TLabel")
        self.header_badge.pack(anchor="w")
        ui_theme.ThemedLabel(
            footer, self.theme, "MIT License · docs/pi-agent-integration.md",
            fg_key="text_faint", bg_key="surface_alt", size=8,
        ).pack(anchor="w", pady=(3, 0))

    def _build_main(self, main: tk.Widget) -> None:
        p = self.theme.palette
        nav = tk.Frame(main, bd=0, highlightthickness=0, bg=p["bg"])
        nav.grid(row=0, column=0, sticky="we", padx=18, pady=(14, 8))
        self.nav_agent_btn = ui_theme.NavItem(
            nav, self.theme, "Agent 助手", lambda: self._select_page("agent"), surface_key="bg"
        )
        self.nav_agent_btn.pack(side="left")
        self.nav_legacy_btn = ui_theme.NavItem(
            nav, self.theme, "传统流程", lambda: self._select_page("legacy"), surface_key="bg"
        )
        self.nav_legacy_btn.pack(side="left", padx=(6, 0))
        # 页面容器：用 tkraise 切换（避免 ttk.Notebook 页签行破坏整体视觉）
        self._page_container = tk.Frame(main, bd=0, highlightthickness=0, bg=p["bg"])
        self._page_container.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 14))
        self._page_container.columnconfigure(0, weight=1)
        self._page_container.rowconfigure(0, weight=1)
        self.agent_tab = ttk.Frame(self._page_container, style="TFrame")
        self.legacy_tab = ttk.Frame(self._page_container, style="TFrame")
        self.agent_tab.grid(row=0, column=0, sticky="nsew")
        self.legacy_tab.grid(row=0, column=0, sticky="nsew")
        # 仅保存页签元数据（自动化测试 / 状态查询用），不参与实际布局
        self.notebook = ttk.Notebook(main, style="Hidden.TNotebook")
        meta_agent = ttk.Frame(self.notebook)
        meta_legacy = ttk.Frame(self.notebook)
        self.notebook.add(meta_agent, text="Agent 助手（pi）")
        self.notebook.add(meta_legacy, text="传统流程（兼容）")
        self._build_agent_tab(self.agent_tab)
        self._build_legacy_tab(self.legacy_tab)
        self._select_page("agent")

    def _select_page(self, name: str) -> None:
        try:
            index = 0 if name == "agent" else 1
            self.notebook.select(index)
            if index == 0:
                self.agent_tab.tkraise(self.legacy_tab)
            else:
                self.legacy_tab.tkraise(self.agent_tab)
            self.nav_agent_btn.set_active(index == 0)
            self.nav_legacy_btn.set_active(index == 1)
        except (AttributeError, tk.TclError):
            pass

    def _on_tab_changed(self, _event=None) -> None:
        try:
            index = self.notebook.index(self.notebook.select())
        except (AttributeError, tk.TclError):
            return
        self._select_page("agent" if index == 0 else "legacy")

    def _build_statusbar(self) -> None:
        p = self.theme.palette
        wrap = tk.Frame(self.root, bd=0, highlightthickness=0, bg=p["surface"])
        wrap.grid(row=2, column=0, sticky="we")
        ui_theme.Divider(wrap, self.theme).pack(fill="x")
        bar = ui_theme.ThemedPanel(wrap, self.theme, bg_key="surface", height=30)
        bar.pack(fill="x")
        bar.grid_propagate(False)
        bar.columnconfigure(1, weight=1)
        self.statusbar_label = ui_theme.ThemedLabel(
            bar, self.theme, "就绪", fg_key="text_muted", bg_key="surface", size=9
        )
        self.statusbar_label.grid(row=0, column=0, sticky="w", padx=16, pady=5)
        self.conn_label = ui_theme.ThemedLabel(
            bar, self.theme, "○ 密钥未配置", fg_key="text_faint", bg_key="surface", size=9
        )
        self.conn_label.grid(row=0, column=2, sticky="e", padx=16, pady=5)

    # -- 主题 -----------------------------------------------------------
    @classmethod
    def _read_theme_name(cls) -> str:
        try:
            data = json.loads(cls._ui_settings_path().read_text(encoding="utf-8"))
            name = str(data.get("theme") or "dark") if isinstance(data, dict) else "dark"
        except (OSError, ValueError, AttributeError):
            return "dark"
        return name if name in ("dark", "light") else "dark"

    def _toggle_theme(self) -> None:
        self._apply_theme("light" if self._theme_name == "dark" else "dark", save=True)

    def _apply_theme(self, name: str, *, save: bool = True, rebuild_styles: bool = True) -> None:
        self._theme_name = name if name in ("dark", "light") else "dark"
        self.theme.set_name(self._theme_name)
        p = self.theme.palette
        try:
            self.root.configure(bg=p["bg"])
        except tk.TclError:
            pass
        if rebuild_styles:
            ui_theme.configure_ttk(ttk.Style(self.root), self.theme)
        self.theme.apply_all()
        try:
            self.root.option_add("*TCombobox*Listbox.background", p["card"])
            self.root.option_add("*TCombobox*Listbox.foreground", p["text"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")
        except tk.TclError:
            pass
        if getattr(self, "btn_theme", None) is not None:
            self.btn_theme.configure(text="◐ 浅色" if self._theme_name == "dark" else "◐ 深色")
        self._update_banner_color()
        self._update_conn_label()
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass
        if save:
            self._save_ui_settings()

    def _toggle_api_visibility(self) -> None:
        try:
            hidden = str(self.api_key_entry.cget("show")) == "*"
            self.api_key_entry.configure(show="" if hidden else "*")
            self.btn_api_toggle.configure(text="隐藏" if hidden else "显示")
        except tk.TclError:
            pass

    def _on_api_key_changed(self) -> None:
        self._update_conn_label()

    def _update_conn_label(self) -> None:
        label = getattr(self, "conn_label", None)
        if label is None:
            return
        try:
            has_key = bool(self.api_key_var.get().strip())
        except tk.TclError:
            has_key = False
        label.configure(text="● 密钥已配置" if has_key else "○ 密钥未配置")
        label.set_fg_key("ok" if has_key else "text_faint")

    def _set_status_state(self, state: str, text: str | None = None) -> None:
        mapping = {
            "idle": ("空闲", "text_muted", "surface_alt"),
            "running": ("运行中", "accent", "accent_soft"),
            "ok": ("就绪", "ok", "ok_soft"),
            "error": ("错误", "error", "error_soft"),
        }
        label, fg, bg = mapping.get(state, mapping["idle"])
        pill = getattr(self, "status_pill", None)
        if pill is not None:
            try:
                pill.set(text or label, fg_key=fg, bg_key=bg)
            except tk.TclError:
                pass
        bar = getattr(self, "statusbar_label", None)
        if bar is not None and text:
            bar.configure(text=self._shorten(text, 140))

    def _update_banner_color(self) -> None:
        banner = getattr(self, "agent_banner", None)
        if banner is None:
            return
        state = getattr(self, "_banner_state", "info")
        banner.set_fg_key({"ok": "ok", "error": "error", "warn": "warn"}.get(state, "text_muted"))
    # ------------------------------------------------------------------
    # Agent 助手标签页
    # ------------------------------------------------------------------

    def _build_agent_tab(self, tab: ttk.Frame) -> None:
        p = self.theme.palette
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)

        banner_card = ui_theme.Card(tab, self.theme, padding=(14, 10))
        banner_card.grid(row=0, column=0, sticky="we", pady=(0, 8))
        self.agent_banner = ui_theme.ThemedLabel(
            banner_card.body, self.theme, "正在检测 pi 运行时…",
            fg_key="text_muted", bg_key="card", size=9, justify="left",
        )
        self.agent_banner.pack(fill="x")
        self._banner_state = "info"
        tab.bind("<Configure>", self._on_agent_tab_configure)

        actions = ui_theme.Card(tab, self.theme, padding=(12, 8))
        actions.grid(row=1, column=0, sticky="we", pady=(0, 8))
        row = tk.Frame(actions.body, bd=0, highlightthickness=0, bg=p["card"])
        row.pack(fill="x")
        self.btn_agent_outline = ttk.Button(
            row, text="提取大纲", style="Primary.TButton", command=self.agent_run_outline
        )
        self.btn_agent_outline.pack(side="left", padx=(0, 8))
        self.btn_agent_polish = ttk.Button(
            row, text="润色当前段落", style="Secondary.TButton", command=self.agent_run_polish
        )
        self.btn_agent_polish.pack(side="left", padx=(0, 8))
        self.btn_load_outline = ttk.Button(
            row, text="载入大纲", style="Ghost.TButton", command=self.load_outline_to_editor
        )
        self.btn_load_outline.pack(side="left", padx=(0, 8))
        self.btn_load_output = ttk.Button(
            row, text="载入输出", style="Ghost.TButton", command=self.load_output_to_panel
        )
        self.btn_load_output.pack(side="left")

        content = tk.Frame(tab, bd=0, highlightthickness=0, bg=p["bg"])
        content.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)
        left = tk.Frame(content, bd=0, highlightthickness=0, bg=p["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=2)
        left.rowconfigure(1, weight=1)
        log_card = ui_theme.Card(left, self.theme, title="状态日志", hint="阶段 · 工具 · 错误")
        log_card.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.agent_log_text = self._make_text_with_scroll(
            log_card.body, height=8, readonly=True, mono=True, tag_colors=LOG_TAG_COLORS
        )
        preview_card = ui_theme.Card(
            left, self.theme, title="大纲实时预览", hint="随生成刷新"
        )
        preview_card.grid(row=1, column=0, sticky="nsew")
        self.agent_outline_preview_text = self._make_text_with_scroll(
            preview_card.body, height=4, readonly=True, mono=False
        )
        right = tk.Frame(content, bd=0, highlightthickness=0, bg=p["bg"])
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=1)
        stream_card = ui_theme.Card(right, self.theme, title="流式输出", hint="助手回复")
        stream_card.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self.agent_stream_text = self._make_text_with_scroll(
            stream_card.body, height=8, readonly=True, mono=False
        )
        structure_card = ui_theme.Card(
            right, self.theme, title="章节 / 小标题结构", hint="实时"
        )
        structure_card.grid(row=1, column=0, sticky="nsew")
        self.agent_structure_text = self._make_text_with_scroll(
            structure_card.body, height=6, readonly=True, mono=False
        )

        chat = ui_theme.Card(
            tab, self.theme, title="对话", hint="Ctrl+Enter 发送 · 「停止」可中止任务"
        )
        chat.grid(row=3, column=0, sticky="we")
        body = chat.body
        body.columnconfigure(0, weight=1)
        self.agent_input_text = self._make_text_with_scroll(
            body, height=4, readonly=False, mono=False
        )
        bar = tk.Frame(body, bd=0, highlightthickness=0, bg=p["card"])
        bar.grid(row=1, column=0, sticky="we", pady=(8, 0))
        ui_theme.ThemedLabel(
            bar, self.theme, "Enter 换行 · Ctrl+Enter 发送",
            fg_key="text_faint", bg_key="card", size=9,
        ).pack(side="left")
        self.btn_agent_new = ttk.Button(
            bar, text="新对话", style="Ghost.TButton", command=self.agent_new_session
        )
        self.btn_agent_new.pack(side="right", padx=(8, 0))
        self.btn_agent_stop = ttk.Button(
            bar, text="停止", style="Danger.TButton", command=self.agent_stop, state="disabled"
        )
        self.btn_agent_stop.pack(side="right", padx=(8, 0))
        self.btn_agent_send = ttk.Button(
            bar, text="发送", style="Primary.TButton", command=self.agent_send
        )
        self.btn_agent_send.pack(side="right")
        self.agent_input_text.bind("<Control-Return>", self._on_agent_input_ctrl_enter)

        self._agent_action_buttons = [
            self.btn_agent_outline,
            self.btn_agent_polish,
            self.btn_agent_send,
            self.btn_agent_new,
        ]
        self._update_structure_panel("")

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
        p = self.theme.palette
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=3)
        tab.rowconfigure(3, weight=2)

        note_card = ui_theme.Card(tab, self.theme, padding=(14, 10))
        note_card.grid(row=0, column=0, sticky="we", pady=(0, 8))
        ui_theme.ThemedLabel(
            note_card.body, self.theme,
            "兼容模式：直接调用 generate_outline.py / polish.py（每个任务单次模型调用，"
            "只依赖 requests），不依赖 pi 运行时。",
            fg_key="text_muted", bg_key="card", size=9, justify="left",
        ).pack(fill="x")

        actions = ui_theme.Card(tab, self.theme, padding=(12, 8))
        actions.grid(row=1, column=0, sticky="we", pady=(0, 8))
        row = tk.Frame(actions.body, bd=0, highlightthickness=0, bg=p["card"])
        row.pack(fill="x")
        self.btn_legacy_outline = ttk.Button(
            row, text="提取论文大纲", style="Primary.TButton", command=self.run_outline
        )
        self.btn_legacy_outline.pack(side="left", padx=(0, 8))
        self.btn_legacy_polish = ttk.Button(
            row, text="开始润色", style="Secondary.TButton", command=self.run_polish
        )
        self.btn_legacy_polish.pack(side="left", padx=(0, 8))
        ttk.Button(
            row, text="加载大纲", style="Ghost.TButton", command=self.load_outline_to_editor
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            row, text="保存大纲", style="Ghost.TButton", command=self.save_outline_from_editor
        ).pack(side="left")

        mid = tk.Frame(tab, bd=0, highlightthickness=0, bg=p["bg"])
        mid.grid(row=2, column=0, sticky="nsew")
        mid.columnconfigure(0, weight=3)
        mid.columnconfigure(1, weight=2)
        mid.rowconfigure(0, weight=1)
        outline_card = ui_theme.Card(mid, self.theme, title="大纲可视化", hint="可编辑")
        outline_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.outline_text = self._make_text_with_scroll(
            outline_card.body, height=8, readonly=False, mono=False
        )
        input_card = ui_theme.Card(mid, self.theme, title="待润色文字", hint="原文")
        input_card.grid(row=0, column=1, sticky="nsew")
        self.input_text = self._make_text_with_scroll(
            input_card.body, height=8, readonly=False, mono=False
        )

        bottom = tk.Frame(tab, bd=0, highlightthickness=0, bg=p["bg"])
        bottom.grid(row=3, column=0, sticky="nsew", pady=(8, 0))
        bottom.columnconfigure(0, weight=3)
        bottom.columnconfigure(1, weight=2)
        bottom.rowconfigure(0, weight=1)
        output_card = ui_theme.Card(bottom, self.theme, title="润色后输出", hint="结果")
        output_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.output_text = self._make_text_with_scroll(
            output_card.body, height=6, readonly=False, mono=False
        )
        status_card = ui_theme.Card(bottom, self.theme, title="状态", hint="日志")
        status_card.grid(row=0, column=1, sticky="nsew")
        self.status_text = self._make_text_with_scroll(
            status_card.body, height=6, readonly=False, mono=True
        )

    # ------------------------------------------------------------------
    # 通用控件工具
    # ------------------------------------------------------------------

    def _make_text_with_scroll(
        self,
        parent: tk.Widget,
        height: int = 8,
        readonly: bool = False,
        width: int = 80,
        *,
        mono: bool | None = None,
        tag_colors: dict | None = None,
    ) -> tk.Text:
        if mono is None:
            mono = readonly
        box = ui_theme.BorderedText(
            parent,
            self.theme,
            height=height,
            readonly=readonly,
            mono=mono,
            tag_colors=tag_colors,
        )
        box.grid(row=0, column=0, sticky="nsew")
        try:
            parent.columnconfigure(0, weight=1)
            parent.rowconfigure(0, weight=1)
        except (AttributeError, tk.TclError):
            pass
        return box.text

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
        bar = getattr(self, "statusbar_label", None)
        if bar is not None:
            line = ""
            for candidate in (text or "").strip().splitlines():
                if candidate.strip():
                    line = candidate.strip()
            if line:
                try:
                    bar.configure(text=self._shorten(line, 140))
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
        self._update_structure_panel(content)
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
    def _run_subprocess(self, cmd: list[str], on_success=None, on_finish=None) -> None:
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
        finally:
            if on_finish:
                self._call_ui(on_finish)

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

        def on_finish() -> None:
            self._legacy_outline_running = False
            self._stop_outline_watch()

        self._legacy_outline_running = True
        self._start_outline_watch()
        threading.Thread(
            target=self._run_subprocess,
            args=(cmd, on_success, on_finish),
            daemon=True,
        ).start()

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
        data: dict = {}
        try:
            existing = json.loads(self._ui_settings_path().read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                data.update(existing)
        except (OSError, ValueError):
            pass
        if model:
            data["model"] = model
        data["theme"] = getattr(self, "_theme_name", "dark")
        try:
            path = self._ui_settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
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
        text = (
            "账户额度：可用 "
            f"{_fmt(payload.get('available_balance'))}（现金 {_fmt(payload.get('cash_balance'))}，"
            f"代金券 {_fmt(payload.get('voucher_balance'))}）"
        )
        self.append_agent_log(text, tag="ok")
        label = getattr(self, "balance_label", None)
        if label is not None:
            try:
                label.configure(text=f"额度：可用 {_fmt(payload.get('available_balance'))}")
                label.set_fg_key("ok")
            except tk.TclError:
                pass

    def _on_balance_failed(self, exc: Exception) -> None:
        self._balance_loading = False
        self._set_balance_button_state("disabled" if self._agent_busy else "normal")
        self.append_agent_log(
            f"⚠️ 查询额度失败：{self._shorten(str(exc), 300)}", tag="error"
        )
        label = getattr(self, "balance_label", None)
        if label is not None:
            try:
                label.configure(text="额度：查询失败")
                label.set_fg_key("error")
            except tk.TclError:
                pass

    def _set_doctor_button_state(self, state: str) -> None:
        try:
            self.btn_doctor.configure(state=state)
        except (AttributeError, tk.TclError):
            pass

    def run_doctor(self) -> None:
        """运行限流诊断：可用模型列表 / 账户额度 / 最小 chat 请求（排查 429）。"""
        if self._doctor_running:
            return
        api_key = self.api_key_var.get().strip()
        if not api_key:
            self._warn("缺少参数", "请先在顶部输入 kimi API 密钥，再运行诊断。")
            return
        script = self.base_dir / "tools" / "moonshot_doctor.py"
        if not script.exists():
            self._show_error("文件缺失", f"未找到诊断脚本：{script}")
            return
        self._doctor_running = True
        self._set_doctor_button_state("disabled")
        self.append_agent_log("=" * 60, tag="muted")
        self.append_agent_log(
            "🩺 正在运行限流诊断（模型列表 / 账户额度 / 最小 chat 测试）…", tag="muted"
        )
        env = os.environ.copy()
        env["MOONSHOT_API_KEY"] = api_key
        env["PYTHONIOENCODING"] = "utf-8"
        model = self._current_model()
        cmd = [sys.executable, str(script)]
        if model:
            cmd += ["--model", model]

        def worker() -> None:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(self.base_dir),
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                output = ((proc.stdout or "") + (proc.stderr or "")).strip()
                self._call_ui(self._on_doctor_done, output, proc.returncode, None)
            except Exception as exc:  # noqa: BLE001
                self._call_ui(self._on_doctor_done, "", -1, f"{type(exc).__name__}: {exc}")

        threading.Thread(target=worker, name="thesis-doctor", daemon=True).start()

    def _on_doctor_done(self, output, returncode, error=None) -> None:
        self._doctor_running = False
        self._set_doctor_button_state("disabled" if self._agent_busy else "normal")
        if error:
            self.append_agent_log(f"⚠️ 诊断失败：{error}", tag="error")
            return
        lines = [line for line in str(output or "").splitlines() if line.strip()]
        for line in lines[:80]:
            self.append_agent_log(line, tag="muted")
        if len(lines) > 80:
            self.append_agent_log(f"…（输出过长，已截断，共 {len(lines)} 行）", tag="muted")
        tag = "ok" if returncode == 0 else "error"
        self.append_agent_log(
            f"🩺 诊断结束（exit={returncode}）。若仍有 429，请把以上输出发给维护者。",
            tag=tag,
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
                )
            )
            self._banner_state = "ok"
            self._update_banner_color()
            self._set_status_state("ok", "pi 运行时已就绪")
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
            self.agent_banner.configure(text=detail)
            self._banner_state = "error"
            self._update_banner_color()
            self._set_status_state("error", "pi 运行时不可用")
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
        elif kind == "toolcall_start":
            self._start_live_tool(
                str(data.get("toolName") or text or ""), str(data.get("toolCallId") or "")
            )
        elif kind == "toolcall_delta":
            self._append_live_tool_delta(str(text), str(data.get("toolCallId") or ""))
        elif kind == "toolcall_end":
            self._finish_live_tool(str(data.get("toolName") or text or ""))
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
            self._finish_live_tool(name)
        elif kind == "status":
            message = self._event_short_text(data, text, ("text", "message", "status", "phase"))
            if message:
                self.append_agent_log(f"ℹ️ {message}", tag="muted")
            raw_error = data.get("raw_error")
            if raw_error:
                raw_text = str(raw_error)
                if raw_text != self._last_raw_error_logged:
                    self._last_raw_error_logged = raw_text
                    self.append_agent_log(
                        f"   原始错误（完整）：{self._shorten(raw_text, 700)}", tag="muted"
                    )
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
            self._set_status_state("ok", "任务完成")
            self.append_agent_log(f"✅ 任务完成{self._usage_note(usage)}", tag="ok")
            # 按发起时的动作类型自动载入结果文件（写盘由 agent 的 thesis_* 工具完成）
            if action == "outline":
                self._auto_load_outline()
            elif action == "polish":
                self._auto_load_output()
        elif aborted:
            self._set_status_state("idle", "已停止")
            suffix = "（未自动载入结果文件）" if action in ("outline", "polish") else ""
            self.append_agent_log(
                f"⏹ 任务已停止{self._usage_note(usage)}{suffix}。", tag="muted"
            )
        else:
            self._set_status_state("error", "任务失败")
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
        self._stop_outline_watch()

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
        self._set_doctor_button_state(
            "disabled" if (busy or self._doctor_running) else "normal"
        )
        try:
            if busy:
                self._set_status_state("running", "运行中…")
            else:
                self._set_status_state("idle", "空闲")
        except Exception:  # noqa: BLE001 - 状态栏为纯展示，失败不影响业务
            pass

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
        self._last_raw_error_logged = ""
        self._live_tool = None
        self._live_args = ""
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
        if kind == "outline":
            self._start_outline_watch()
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
        self._live_tool = None
        self._live_args = ""
        self._stop_outline_watch()
        self._update_structure_panel("")
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
        self._update_structure_panel(content)
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
    # 实时预览：tool 参数流 → 大纲编辑器 / 小标题面板
    # ------------------------------------------------------------------
    LIVE_TOOLS = ("thesis_outline_save", "thesis_save_polished")

    @staticmethod
    def _extract_partial_json_string(raw: str, key: str) -> str:
        """从（可能不完整的）JSON 文本里容错提取某字符串字段的当前内容。

        用于实时预览：模型流式输出工具参数时，``content``/``text`` 字段可能尚未闭合。
        """
        marker = f'"{key}"'
        idx = raw.find(marker)
        if idx < 0:
            return ""
        colon = raw.find(":", idx + len(marker))
        if colon < 0:
            return ""
        i = colon + 1
        while i < len(raw) and raw[i] in " \t\r\n":
            i += 1
        if i >= len(raw) or raw[i] != '"':
            return ""
        i += 1
        out: list[str] = []
        escapes = {
            "n": "\n",
            "t": "\t",
            "r": "\r",
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
        }
        while i < len(raw):
            ch = raw[i]
            if ch == "\\":
                if i + 1 >= len(raw):
                    break
                esc = raw[i + 1]
                if esc == "u":
                    hex_part = raw[i + 2 : i + 6]
                    if len(hex_part) < 4:
                        break
                    try:
                        out.append(chr(int(hex_part, 16)))
                    except ValueError:
                        break
                    i += 6
                    continue
                out.append(escapes.get(esc, esc))
                i += 2
                continue
            if ch == '"':
                break
            out.append(ch)
            i += 1
        return "".join(out)

    def _start_live_tool(self, name: str, tool_call_id: str) -> None:
        if name not in self.LIVE_TOOLS:
            self._live_tool = None
            self._live_args = ""
            return
        self._live_tool = {"id": str(tool_call_id or ""), "name": name}
        self._live_args = ""
        self._live_last_update = 0.0
        self.append_agent_log(
            f"✍️ {name} 参数生成中：大纲/润色内容将实时显示…", tag="muted"
        )

    def _append_live_tool_delta(self, delta: str, tool_call_id: str = "") -> None:
        live = self._live_tool
        if not live or not delta:
            return
        if tool_call_id and live.get("id") and tool_call_id != live.get("id"):
            return
        self._live_args += delta
        now = time.monotonic()
        if now - self._live_last_update >= 0.12:
            self._live_last_update = now
            self._apply_live_tool_args()

    def _apply_live_tool_args(self) -> None:
        live = self._live_tool
        if not live:
            return
        key = "content" if live["name"] == "thesis_outline_save" else "text"
        partial = self._extract_partial_json_string(self._live_args, key)
        if not partial:
            return
        try:
            if key == "content":
                self._set_text_content(self.outline_text, partial)
                self._update_structure_panel(partial)
            else:
                self._set_text_content(self.output_text, partial)
        except tk.TclError:
            return

    def _finish_live_tool(self, name: str = "") -> None:
        if self._live_tool and (not name or name == self._live_tool.get("name")):
            self._apply_live_tool_args()
        self._live_tool = None
        self._live_args = ""

    @staticmethod
    def _parse_outline_structure(text: str) -> list[tuple[str, list[str]]]:
        """解析大纲文本为 [(章标题, [小标题...]), ...]（小标题为章节行下 2 空格缩进行）。"""
        chapter_re = re.compile(r"^(第[一二三四五六七八九十百零两0-9]+章)题目[：:](.*)$")
        chapters: list[tuple[str, list[str]]] = []
        current: list[str] | None = None
        for raw_line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if not raw_line.strip():
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            stripped = raw_line.strip()
            if indent < 2:
                match = chapter_re.match(stripped)
                if match:
                    title = f"{match.group(1)} {match.group(2).strip()}".strip()
                    current = []
                    chapters.append((title, current))
                    continue
                if re.match(r"^(题目|摘要|总结|参考文献)[：:]", stripped):
                    current = None
                # 其它未知行忽略、不打断当前章节的小标题关联（容忍流式不完整内容）
                continue
            if current is not None:
                current.append(stripped)
        return chapters

    def _update_structure_panel(self, outline_text: str | None = None) -> None:
        widget = getattr(self, "agent_structure_text", None)
        if widget is None:
            return
        if outline_text is None:
            try:
                outline_text = self._get_text_content(self.outline_text)
            except tk.TclError:
                outline_text = ""
        chapters = self._parse_outline_structure(outline_text)
        lines: list[str] = []
        for title, children in chapters:
            lines.append(title)
            for child in children:
                lines.append(f"    └ {child}")
        if not lines:
            lines.append("（等待 agent 生成或载入大纲后，这里实时显示章节与下一级小标题）")
        else:
            total = sum(len(children) for _, children in chapters)
            lines.append("")
            lines.append(f"共 {len(chapters)} 章 / {total} 条小标题")
        try:
            self._set_text_content(widget, "\n".join(lines), readonly=True)
            widget.see("1.0")
        except tk.TclError:
            pass
        preview = getattr(self, "agent_outline_preview_text", None)
        if preview is not None:
            try:
                self._set_text_content(preview, outline_text or "", readonly=True)
                preview.see("1.0")
            except tk.TclError:
                pass

    # ------------------------------------------------------------------
    # 实时文件监听：outline.txt 一有变化就载入编辑器 / 刷新区块结构面板
    # ------------------------------------------------------------------
    OUTLINE_WATCH_MS = 600

    def _start_outline_watch(self) -> None:
        if self._outline_watch_job is not None:
            return
        try:
            self._outline_watch_mtime = self._outline_path().stat().st_mtime
        except OSError:
            self._outline_watch_mtime = None
        try:
            self._outline_watch_job = self.root.after(
                self.OUTLINE_WATCH_MS, self._poll_outline_file
            )
        except tk.TclError:
            self._outline_watch_job = None

    def _stop_outline_watch(self) -> None:
        if self._outline_watch_job is not None:
            try:
                self.root.after_cancel(self._outline_watch_job)
            except (tk.TclError, ValueError):
                pass
            self._outline_watch_job = None
        self._outline_watch_mtime = None

    def _poll_outline_file(self) -> None:
        self._outline_watch_job = None
        if self._closing:
            return
        path = self._outline_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime is not None and mtime != self._outline_watch_mtime:
            self._outline_watch_mtime = mtime
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                content = ""
            if content.strip():
                self._set_text_content(self.outline_text, content)
                self._update_structure_panel(content)
                self.append_status("实时载入 outline.txt（含章节小标题）")
        still_watching = self._agent_busy or self._legacy_outline_running
        if still_watching and not self._closing:
            try:
                self._outline_watch_job = self.root.after(
                    self.OUTLINE_WATCH_MS, self._poll_outline_file
                )
            except tk.TclError:
                self._outline_watch_job = None
        else:
            self._outline_watch_mtime = None

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
        self._stop_outline_watch()

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
    ui_theme.enable_dpi_awareness()
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
