# -*- coding: utf-8 -*-
"""ThesisPolish 现代 UI 设计体系（tkinter / ttk）。

集中管理：
- 调色板（深色 / 浅色）与字体
- ttk 样式（按钮 / 输入框 / 滚动条 / 隐藏页签的 Notebook 等）
- 可主题化的自定义控件（卡片 Card、胶囊 Pill、导航项 NavItem、带边框文本框 ThemedText 等）

设计 token 全部集中在 ``PALETTES``，切换主题时 ``Theme.set_name()`` + ``configure_ttk()`` +
各控件 ``apply_palette()`` 即可整体换肤，无需重建界面。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont, ttk

# ---------------------------------------------------------------------------
# 设计 token：8pt 间距体系、靛蓝主色
# ---------------------------------------------------------------------------
PALETTES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0B0D12",            # 窗口背景
        "surface": "#12151C",       # 顶栏 / 状态栏
        "surface_alt": "#0E1119",   # 侧边栏
        "card": "#151A23",          # 卡片
        "card_alt": "#101521",
        "border": "#232A38",
        "border_soft": "#1B2130",
        "text": "#E6E9F0",
        "text_muted": "#8B93A7",
        "text_faint": "#5B6478",
        "accent": "#6366F1",
        "accent_hover": "#7C7FF5",
        "accent_soft": "#1E2140",
        "ok": "#22C55E",
        "ok_soft": "#0F2A1B",
        "warn": "#F59E0B",
        "warn_soft": "#2B2110",
        "error": "#EF4444",
        "error_soft": "#2C1416",
        "input_bg": "#0D1119",
        "console_bg": "#0A0C11",
        "selection": "#2A2F55",
        "scroll": "#2A3242",
        "scroll_hover": "#3B445A",
        "button_bg": "#1B2130",
        "button_bg_hover": "#242C3E",
        "disabled_fg": "#4B5468",
    },
    "light": {
        "bg": "#F4F6FA",
        "surface": "#FFFFFF",
        "surface_alt": "#EDF0F7",
        "card": "#FFFFFF",
        "card_alt": "#F7F9FC",
        "border": "#D8DEEA",
        "border_soft": "#E5E9F2",
        "text": "#1E2433",
        "text_muted": "#5B6478",
        "text_faint": "#8A93A6",
        "accent": "#4F46E5",
        "accent_hover": "#4338CA",
        "accent_soft": "#EEF0FF",
        "ok": "#15803D",
        "ok_soft": "#E7F6EC",
        "warn": "#B45309",
        "warn_soft": "#FDF3E3",
        "error": "#B91C1C",
        "error_soft": "#FCECEC",
        "input_bg": "#FFFFFF",
        "console_bg": "#F8FAFC",
        "selection": "#C7D2FE",
        "scroll": "#C3CBDA",
        "scroll_hover": "#A9B4C8",
        "button_bg": "#EEF1F7",
        "button_bg_hover": "#E2E7F1",
        "disabled_fg": "#A6AFC0",
    },
}

_UI_FONT_CANDIDATES = (
    "Segoe UI Variable Display",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Segoe UI",
)
_MONO_FONT_CANDIDATES = ("Cascadia Mono", "JetBrains Mono", "Consolas", "Courier New")


def enable_dpi_awareness() -> None:
    """Windows 高 DPI 感知（必须在创建 Tk 之前调用；失败静默）。"""
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
        except Exception:  # noqa: BLE001 - 旧系统回退
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 - 非 Windows / 受限环境忽略
        pass


def _pick_font(root: tk.Misc, candidates: tuple[str, ...], fallback: str) -> str:
    try:
        available = {name.lower() for name in tkfont.families(root)}
    except tk.TclError:
        available = set()
    for name in candidates:
        if name.lower() in available:
            return name
    return fallback


class Theme:
    """主题状态：调色板、字体、控件注册表。"""

    def __init__(self, root: tk.Misc, name: str = "dark"):
        self.root = root
        self.name = name if name in PALETTES else "dark"
        self.ui_family = _pick_font(root, _UI_FONT_CANDIDATES, "TkDefaultFont")
        self.mono_family = _pick_font(root, _MONO_FONT_CANDIDATES, "Courier New")
        self._registry: list[tk.Misc] = []
        self._tag_widgets: list[tk.Text] = []

    # -- 基础 -----------------------------------------------------------
    @property
    def palette(self) -> dict[str, str]:
        return PALETTES[self.name]

    def font(self, size: int = 10, weight: str = "normal") -> tuple:
        return (self.ui_family, size, weight)

    def mono_font(self, size: int = 9, weight: str = "normal") -> tuple:
        return (self.mono_family, size, weight)

    def set_name(self, name: str) -> None:
        self.name = name if name in PALETTES else "dark"

    # -- 控件注册 / 换肤 -------------------------------------------------
    def add(self, widget: tk.Misc) -> None:
        self._registry.append(widget)

    def apply_all(self) -> None:
        palette = self.palette
        self.root.configure(bg=palette["bg"])
        for widget in list(self._registry):
            apply = getattr(widget, "apply_palette", None)
            if apply is None:
                continue
            try:
                if widget.winfo_exists():
                    apply(palette)
            except tk.TclError:
                continue


# ---------------------------------------------------------------------------
# ttk 样式
# ---------------------------------------------------------------------------
def configure_ttk(style: ttk.Style, theme: Theme) -> None:
    p = theme.palette
    ui = theme.ui_family
    mono = theme.mono_family
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=p["bg"], foreground=p["text"], font=(ui, 10))
    style.configure("TFrame", background=p["bg"])
    style.configure("Card.TFrame", background=p["card"])
    style.configure("Sidebar.TFrame", background=p["surface_alt"])
    style.configure("Header.TFrame", background=p["surface"])
    style.configure("Status.TFrame", background=p["surface"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"], font=(ui, 10))
    style.configure("HeaderTitle.TLabel", background=p["surface"], foreground=p["text"], font=(ui, 14, "bold"))
    style.configure("HeaderSub.TLabel", background=p["surface"], foreground=p["text_muted"], font=(ui, 9))
    style.configure("Muted.TLabel", background=p["bg"], foreground=p["text_muted"], font=(ui, 9))
    style.configure("CardMuted.TLabel", background=p["card"], foreground=p["text_muted"], font=(ui, 9))
    style.configure("CardTitle.TLabel", background=p["card"], foreground=p["text"], font=(ui, 11, "bold"))
    style.configure("CardHint.TLabel", background=p["card"], foreground=p["text_muted"], font=(ui, 9))
    style.configure("Field.TLabel", background=p["card"], foreground=p["text_muted"], font=(ui, 9))
    style.configure("Section.TLabel", background=p["bg"], foreground=p["text"], font=(ui, 12, "bold"))
    style.configure("SidebarMuted.TLabel", background=p["surface_alt"], foreground=p["text_faint"], font=(ui, 8))
    style.configure("Status.TLabel", background=p["surface"], foreground=p["text_muted"], font=(ui, 9))
    style.configure("Error.TLabel", background=p["card"], foreground=p["error"], font=(ui, 9))

    # 按钮：主 / 次 / 幽灵 / 危险 / 小尺寸
    def _button(name: str, bg: str, fg: str, hover: str, *, border: str | None = None) -> None:
        style.configure(
            name,
            background=bg,
            foreground=fg,
            borderwidth=1 if border else 0,
            bordercolor=border if border else bg,
            lightcolor=bg,
            darkcolor=bg,
            relief="flat",
            padding=(14, 8),
            font=(ui, 10),
            anchor="center",
        )
        style.map(
            name,
            background=[("pressed", hover), ("active", hover), ("disabled", p["button_bg"])],
            foreground=[("disabled", p["disabled_fg"])],
            bordercolor=[("active", border or hover), ("disabled", p["border_soft"])],
        )

    _button("Primary.TButton", p["accent"], "#FFFFFF", p["accent_hover"])
    _button("Secondary.TButton", p["button_bg"], p["text"], p["button_bg_hover"], border=p["border"])
    _button("Ghost.TButton", p["card"], p["text_muted"], p["button_bg"])
    _button("Danger.TButton", p["error_soft"], p["error"], p["error_soft"], border=p["error"])
    _button("Nav.TButton", p["surface_alt"], p["text_muted"], p["button_bg"])
    style.configure("Small.TButton", padding=(9, 5), font=(ui, 9))
    style.configure("SmallPrimary.TButton", padding=(10, 6), font=(ui, 9))

    # 输入框 / 下拉框
    for name in ("TEntry", "Card.TEntry"):
        style.configure(
            name,
            fieldbackground=p["input_bg"],
            foreground=p["text"],
            insertcolor=p["text"],
            bordercolor=p["border"],
            lightcolor=p["border"],
            darkcolor=p["border"],
            padding=(8, 6),
            relief="flat",
        )
        style.map(
            name,
            fieldbackground=[("disabled", p["card_alt"]), ("readonly", p["card_alt"])],
            foreground=[("disabled", p["disabled_fg"])],
            bordercolor=[("focus", p["accent"]), ("hover", p["border"])],
        )
    style.configure(
        "TCombobox",
        fieldbackground=p["input_bg"],
        background=p["button_bg"],
        foreground=p["text"],
        arrowcolor=p["text_muted"],
        bordercolor=p["border"],
        lightcolor=p["border"],
        darkcolor=p["border"],
        padding=(8, 6),
        relief="flat",
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", p["input_bg"]), ("disabled", p["card_alt"])],
        foreground=[("disabled", p["disabled_fg"])],
        bordercolor=[("focus", p["accent"]), ("hover", p["border"])],
        arrowcolor=[("active", p["accent"])],
    )

    # 滚动条
    for orient in ("Vertical", "Horizontal"):
        name = f"{orient}.TScrollbar"
        style.configure(
            name,
            background=p["scroll"],
            troughcolor=p["console_bg"],
            bordercolor=p["console_bg"],
            arrowcolor=p["text_faint"],
            lightcolor=p["scroll"],
            darkcolor=p["scroll"],
            arrowsize=12,
            width=10,
        )
        style.map(name, background=[("active", p["scroll_hover"]), ("pressed", p["accent"])])

    # Notebook：隐藏页签行（导航由左侧/顶部自定义按钮驱动）
    style.configure("Hidden.TNotebook", background=p["bg"], borderwidth=0, tabmargins=0)
    try:
        style.layout("Hidden.TNotebook", [("Notebook.client", {"sticky": "nswe"})])
    except tk.TclError:
        pass
    style.configure("TNotebook", background=p["bg"], borderwidth=0)
    style.configure("TPanedwindow", background=p["bg"])
    style.configure("Body.TPanedwindow", background=p["bg"], sashwidth=6)
    try:
        style.configure("Sash", background=p["border"], sashthickness=6)
    except tk.TclError:
        pass
    style.configure(
        "TCheckbutton",
        background=p["card"],
        foreground=p["text"],
        indicatorcolor=p["input_bg"],
        font=(ui, 9),
    )
    style.map(
        "TCheckbutton",
        background=[("active", p["card"])],
        indicatorcolor=[("selected", p["accent"])],
    )
    style.configure(
        "TProgressbar",
        background=p["accent"],
        troughcolor=p["card_alt"],
        bordercolor=p["card_alt"],
        lightcolor=p["accent"],
        darkcolor=p["accent"],
    )
    style.configure("TSeparator", background=p["border"])


# ---------------------------------------------------------------------------
# 自定义主题化控件
# ---------------------------------------------------------------------------
def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float, radius: float, **kwargs):
    """在 Canvas 上画圆角矩形（返回 item id）。"""
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class ThemedPanel(tk.Frame):
    """纯色面板（顶栏 / 状态栏 / 侧边栏 / 分隔线）。"""

    def __init__(self, parent: tk.Misc, theme: Theme, bg_key: str = "bg", **kwargs):
        super().__init__(parent, bd=0, highlightthickness=0, **kwargs)
        self._theme = theme
        self._bg_key = bg_key
        theme.add(self)
        self.apply_palette(theme.palette)

    def apply_palette(self, p: dict[str, str]) -> None:
        self.configure(bg=p[self._bg_key])


class Divider(tk.Frame):
    """1px 分隔线（可水平/垂直）。"""

    def __init__(self, parent: tk.Misc, theme: Theme, orient: str = "h", key: str = "border"):
        super().__init__(parent, bd=0, highlightthickness=0, height=1 if orient == "h" else 1)
        self._theme = theme
        self._key = key
        theme.add(self)
        self.apply_palette(theme.palette)

    def apply_palette(self, p: dict[str, str]) -> None:
        self.configure(bg=p[self._key])


class ThemedLabel(tk.Label):
    """主题化 tk.Label（用于需要精确控制颜色的文字）。"""

    def __init__(
        self, parent: tk.Misc,
        theme: Theme,
        text: str = "",
        *,
        fg_key: str = "text",
        bg_key: str = "card",
        size: int = 9,
        weight: str = "normal",
        mono: bool = False,
        **kwargs,
    ):
        kwargs.setdefault("anchor", "w")
        kwargs.setdefault("justify", "left")
        super().__init__(parent, text=text, bd=0, highlightthickness=0, **kwargs)
        self._theme = theme
        self._fg_key = fg_key
        self._bg_key = bg_key
        self._size = size
        self._weight = weight
        self._mono = mono
        theme.add(self)
        self.apply_palette(theme.palette)

    def set_fg_key(self, key: str) -> None:
        self._fg_key = key
        self.apply_palette(self._theme.palette)

    def apply_palette(self, p: dict[str, str]) -> None:
        self.configure(
            bg=p[self._bg_key],
            fg=p[self._fg_key],
            font=(
                (self._theme.mono_family if self._mono else self._theme.ui_family),
                self._size,
                self._weight,
            ),
        )


class Card(tk.Frame):
    """卡片容器：1px 描边 + 可选标题/提示 + 内容区 body。"""

    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        *,
        title: str | None = None,
        hint: str | None = None,
        padding: tuple[int, int] = (14, 12),
    ):
        super().__init__(parent, bd=0, highlightthickness=1, highlightbackground=theme.palette["border"], bg=theme.palette["card"])
        self._theme = theme
        self._title_text = title
        self._hint_text = hint
        self._padding = padding
        theme.add(self)

        self.columnconfigure(0, weight=1)
        row = 0
        self._title_label: tk.Label | None = None
        self._hint_label: tk.Label | None = None
        if title:
            head = tk.Frame(self, bd=0, highlightthickness=0, bg=theme.palette["card"])
            head.grid(row=row, column=0, sticky="we", padx=(padding[0], padding[0]), pady=(padding[1], 0))
            head.columnconfigure(0, weight=1)
            self._head = head
            self._title_label = tk.Label(
                head, text=title, bd=0, highlightthickness=0, anchor="w", justify="left"
            )
            self._title_label.grid(row=0, column=0, sticky="w")
            if hint:
                self._hint_label = tk.Label(
                    head, text=hint, bd=0, highlightthickness=0, anchor="e", justify="right"
                )
                self._hint_label.grid(row=0, column=1, sticky="e")
            row += 1
        self.body = tk.Frame(self, bd=0, highlightthickness=0, bg=theme.palette["card"])
        self.body.grid(row=row, column=0, sticky="nsew", padx=padding[0], pady=padding[1])
        self.body.columnconfigure(0, weight=1)
        self.rowconfigure(row, weight=1)
        self.apply_palette(theme.palette)

    def apply_palette(self, p: dict[str, str]) -> None:
        self.configure(bg=p["card"], highlightbackground=p["border"])
        self.body.configure(bg=p["card"])
        if hasattr(self, "_head"):
            self._head.configure(bg=p["card"])
        if self._title_label is not None:
            self._title_label.configure(bg=p["card"], fg=p["text"], font=self._theme.font(11, "bold"))
        if self._hint_label is not None:
            self._hint_label.configure(bg=p["card"], fg=p["text_muted"], font=self._theme.font(9))


class Pill(tk.Canvas):
    """圆角状态胶囊（状态 / 标签）。"""

    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        text: str = "",
        *,
        fg_key: str = "text_muted",
        bg_key: str = "surface_alt",
        surface_key: str = "surface",
        size: int = 9,
        height: int = 24,
    ):
        super().__init__(parent, height=height, bd=0, highlightthickness=0)
        self._theme = theme
        self._text = text
        self._fg_key = fg_key
        self._bg_key = bg_key
        self._surface_key = surface_key
        self._size = size
        self._height = height
        theme.add(self)
        self.apply_palette(theme.palette)

    def set(self, text: str, fg_key: str | None = None, bg_key: str | None = None) -> None:
        self._text = text
        if fg_key:
            self._fg_key = fg_key
        if bg_key:
            self._bg_key = bg_key
        self.apply_palette(self._theme.palette)

    def apply_palette(self, p: dict[str, str]) -> None:
        self.configure(bg=p[self._surface_key])
        self.delete("all")
        font = self._theme.font(self._size, "bold")
        probe = tkfont.Font(root=self, font=font)
        width = probe.measure(self._text) + 22
        self.configure(width=width, height=self._height)
        rounded_rect(
            self, 0.5, 0.5, width - 0.5, self._height - 0.5, (self._height - 1) / 2,
            fill=p[self._bg_key], outline=p["border"], width=1,
        )
        self.create_text(width / 2, self._height / 2, text=self._text, fill=p[self._fg_key], font=font)


class NavItem(tk.Label):
    """自定义导航项（左侧/顶部导航，点击切换页面）。"""

    def __init__(self, parent: tk.Misc, theme: Theme, text: str, command, *, surface_key: str = "surface"):
        super().__init__(parent, text=text, bd=0, highlightthickness=0, padx=16, pady=8, cursor="hand2", anchor="w")
        self._theme = theme
        self._command = command
        self._surface_key = surface_key
        self._active = False
        self._hover = False
        theme.add(self)
        self.bind("<Button-1>", lambda _e: self._command())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.apply_palette(theme.palette)

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self.apply_palette(self._theme.palette)

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self.apply_palette(self._theme.palette)

    def set_active(self, active: bool) -> None:
        self._active = active
        self.apply_palette(self._theme.palette)

    def apply_palette(self, p: dict[str, str]) -> None:
        surface = p[self._surface_key]
        if self._active:
            bg, fg = p["accent"], "#FFFFFF"
        elif self._hover:
            bg, fg = p["button_bg"], p["text"]
        else:
            bg, fg = surface, p["text_muted"]
        self.configure(bg=bg, fg=fg, font=self._theme.font(10, "bold" if self._active else "normal"))


class Brand(tk.Canvas):
    """左上角品牌标识：圆角方块 + 字母缩写。"""

    def __init__(self, parent: tk.Misc, theme: Theme, *, size: int = 34, letters: str = "TP"):
        super().__init__(parent, width=size, height=size, bd=0, highlightthickness=0)
        self._theme = theme
        self._size = size
        self._letters = letters
        theme.add(self)
        self.apply_palette(theme.palette)

    def apply_palette(self, p: dict[str, str]) -> None:
        self.configure(bg=p["surface"])
        self.delete("all")
        size = self._size
        rounded_rect(self, 1, 1, size - 1, size - 1, 9, fill=p["accent"], outline=p["accent_hover"], width=1)
        self.create_text(
            size / 2, size / 2 + 1, text=self._letters, fill="#FFFFFF",
            font=self._theme.font(13, "bold"),
        )


class ThemedText(tk.Text):
    """主题化多行文本（控制台 / 编辑器），支持标签配色随主题切换。"""

    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        *,
        height: int = 8,
        readonly: bool = False,
        mono: bool = False,
        width: int = 60,
        tag_colors: dict[str, tuple[str, str | None]] | None = None,
        **kwargs,
    ):
        self._theme = theme
        self._readonly = readonly
        self._mono = mono
        self._tag_colors = dict(tag_colors or {})
        super().__init__(
            parent,
            height=height,
            width=width,
            wrap="word",
            undo=not readonly,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=10,
            pady=8,
            insertwidth=2,
            **kwargs,
        )
        theme.add(self)
        self.apply_palette(theme.palette)
        if readonly:
            self.configure(state="disabled")

    def set_tag_colors(self, tag_colors: dict[str, tuple[str, str | None]]) -> None:
        self._tag_colors = dict(tag_colors)
        self.apply_palette(self._theme.palette)

    def apply_palette(self, p: dict[str, str]) -> None:
        bg_key = "console_bg" if self._readonly or self._mono else "input_bg"
        self.configure(
            bg=p[bg_key],
            fg=p["text"],
            insertbackground=p["text"],
            selectbackground=p["selection"],
            selectforeground=p["text"],
            font=(self._theme.mono_family if self._mono else self._theme.ui_family, 10)
            if not self._mono
            else self._theme.mono_font(9),
        )
        for tag, colors in self._tag_colors.items():
            fg_key, bg_key2 = colors
            try:
                if bg_key2:
                    self.tag_configure(tag, foreground=p[fg_key], background=p[bg_key2])
                else:
                    self.tag_configure(tag, foreground=p[fg_key], background="")
            except tk.TclError:
                pass


class BorderedText(tk.Frame):
    """带 1px 描边的文本区（滚动条 + ThemedText），返回 .text 供调用方使用。"""

    def __init__(
        self,
        parent: tk.Misc,
        theme: Theme,
        *,
        height: int = 8,
        readonly: bool = False,
        mono: bool = False,
        tag_colors: dict[str, tuple[str, str | None]] | None = None,
    ):
        super().__init__(parent, bd=0, highlightthickness=1, highlightbackground=theme.palette["border"], bg=theme.palette["card"])
        self._theme = theme
        theme.add(self)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = ThemedText(
            self, theme, height=height, readonly=readonly, mono=mono, tag_colors=tag_colors
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        self.scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        self.scroll.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4)
        self.text.configure(yscrollcommand=self.scroll.set)
        self.apply_palette(theme.palette)

    def apply_palette(self, p: dict[str, str]) -> None:
        self.configure(bg=p["card"], highlightbackground=p["border"])


def apply_theme(root: tk.Misc, theme: Theme) -> None:
    """应用一套完整主题：root 背景 + ttk 样式 + 所有自定义控件。"""
    theme.apply_all()
    style = ttk.Style(root)
    configure_ttk(style, theme)
    try:
        root.configure(bg=theme.palette["bg"])
    except tk.TclError:
        pass
