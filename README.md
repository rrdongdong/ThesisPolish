***
# Openwheel
Open-source some readily usable tools. At present, I don't know what to write about.
- ThesisPolish
- ...
***

# Project1 : ThesisPolish

## 项目介绍 (Project Introduction)

ThesisPolish 面向硕士毕业论文润色场景：从论文 PDF 提取大纲、维护可编辑的九行大纲、按大纲对局部段落做保义润色。

**v2（2026-10）引入了 [pi](https://github.com/earendil-works/pi) agent 框架**：润色流程不再是一段固定 prompt 的单次调用，而是由本地 pi agent 驱动——它会自己上传 PDF、分块阅读正文、校验大纲模板、把结果写入文件，并把每一步的流式输出/工具调用实时显示在 GUI 里。原有「传统流程」完整保留，可随时回退。

This tool is designed for polishing master's thesis. Since v2 it is **agent-driven**: a local [pi](https://github.com/earendil-works/pi) agent (v0.85.1, Node) orchestrates PDF ingestion, outline validation and polished-section writing through custom tools, while the Python/tkinter GUI streams the agent's progress. The legacy single-shot pipeline is still available as a compatibility tab.

## 主要特性

- **Agent 助手（pi）**：新增标签页，直接和 agent 对话；「提取大纲」「润色当前段落」一键下发任务，实时显示 `text_delta` 流式文本、工具调用、重试与错误。
- **自定义 pi 工具**：`.pi/agent/extensions/thesis-tools.ts` 注册 4 个确定性工具（PDF 正文提取、大纲校验、大纲保存、润色结果保存），文件写入带校验与 `.bak` 备份。
- **Kimi / Moonshot**：使用 pi 内置 `moonshotai-cn` provider 与 `kimi-k2-0905-preview` 模型（262K 上下文），API Key 只在 GUI 输入并注入子进程环境变量，不落盘。
- **Agent 模型可切换**：顶部「Agent 模型」下拉框可用当前 API Key 一键拉取账号可用模型列表（`GET /v1/models`），自动挑选较新的 Kimi 模型；模型 404/无权限时自动刷新并切换，选择会记住在 `.thesis/ui_settings.json`。
- **额度诊断与限流重试**：顶部「查询额度」可查 Moonshot 余额/代金券；429/quota 时 pi 自动重试（`.pi/agent/settings.json`：4 次、**20s 起指数退避**，适配 60 秒 RPM 窗口），重试成功后不会再误报失败；若结果文件已落盘但最后汇报被限流，GUI 会自动载入并提示「任务实际已完成」。距上次 429 不足 60 秒时，快捷任务会**自动等待剩余冷却时间后开始**（重试请求同样占用 RPM），`停止` 15 秒未确认会强制结束 pi 进程。`thesis_pdf_ingest` 返回 `preview/headings/tail_preview`，信息足够时 agent 可不调用 `read`，把大纲任务压到 3 次模型调用。
- **低 RPM 兜底**：若 Moonshot 组织限制很低（如 `max RPM: 3`），可改用「传统流程（兼容）」——每个任务只有 1 次 chat 调用，并把当前「Agent 模型」作为 `--model` 传给 `generate_outline.py` / `polish.py`。
- **长文本安全**：prompt 走 RPC JSON 行 / stdin，绝不放进 Windows 命令行（约 32K 字符上限）；可处理 10 万级字符的论文正文。
- **可取消、可多轮**：`停止` 发送 abort；`新对话` 新建 pi 会话；GUI 关闭时安全回收 pi 子进程。
- **传统流程兼容**：原 `generate_outline.py` / `polish.py` 流程原样保留，缺 pi/Node 时仍可用。
- **离线可验收**：自带 OpenAI + Moonshot Files API 双协议 mock 服务与 98 个 pytest 用例，无需真实 API Key 即可验证完整链路。

## 系统架构

```text
┌──────────────────────────── GUI.py (tkinter) ────────────────────────────┐
│ 共享头部：API Key / Agent 模型 / PDF 路径 / 大纲文件 / 输出文件 / 润色位置 │
│  ├─ Tab 1「Agent 助手（pi）」  ← 默认                                      │
│  └─ Tab 2「传统流程（兼容）」  → generate_outline.py / polish.py（旧逻辑）  │
└───────────────┬──────────────────────────────────────────────────────────┘
                │ on_event 回调只入 queue，主线程 root.after(80ms) 轮询刷新
                ▼
        agent_runtime.py（PiRuntime，stdlib only）
         │  主路径：node <pi-cli.js> --mode rpc  （长驻、流式、abort、多轮）
         │  降级：--mode json 一次性进程（正文经 stdin）
         ▼
   pi 0.85.1 (Node ≥ 22.19)  ── provider: moonshotai-cn / kimi-k2-0905-preview
         │ 文件工具白名单：read, grep, ls, find + thesis_*
         ▼
   .pi/agent/extensions/thesis-tools.ts（TypeScript，pi 自动加载，无需编译）
         │
         ├─ thesis_pdf_ingest  → tools/pdf_ingest.py → Moonshot Files API
         │                       └─ .thesis/pdf_text.txt（正文）+ sha1 缓存
         ├─ thesis_outline_validate → 九行模板确定性校验
         ├─ thesis_outline_save     → outline.txt（校验 + .bak 备份）
         └─ thesis_save_polished    → polished.txt（.bak + .thesis/polish_history.jsonl）
```

设计说明（为什么是 RPC 而不是 SDK）：pi 官方 `docs/sdk.md` 建议非 Node 宿主优先使用 RPC 模式；RPC 长驻进程同时提供流式事件、工具事件、取消与多轮会话，且规避了 Windows argv 长度限制。完整集成说明（架构、工具契约、事件语义、Windows 坑、离线测试策略、升级风险）见 [`docs/pi-agent-integration.md`](docs/pi-agent-integration.md)。

## 环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10/11 | 开发/验证环境；Linux/macOS 未验证 |
| Python | 3.10+（实测 3.12） | tkinter + requests |
| Node.js | ≥ 22.19（实测 24.16） | pi 运行时 |
| pi | 0.85.1（已按此版本验证） | `npm i -g @earendil-works/pi-coding-agent` |
| 网络 | 可访问 `https://api.moonshot.cn` | 仅调用 Kimi API 时需要 |
| API Key | Moonshot/Kimi API Key | 在 GUI 顶部输入，不写入任何文件 |

## 安装

```powershell
# 1) Python 依赖（仅 tools/ 与旧版脚本需要；pi 运行时本身不需要 Python 包）
#    注意用 python -m pip，确保装进当前激活的解释器/conda 环境
python -m pip install -r requirements.txt

# 2) Node 与 pi（若已安装可跳过；确认版本）
node --version                                   # >= 22.19
npm i -g @earendil-works/pi-coding-agent          # 或升级：pi update self
pi --version                                      # 期望 0.85.1

# 3) 验证 pi 内置的 Moonshot provider 可用
$env:MOONSHOT_API_KEY = "sk-..."
pi auth check --provider moonshotai-cn --json    # {"status":"ready", ...}
```

> `pi` 不在 PATH 时，可设置 `THESIS_PI_CLI` 指向
> `...\node_modules\@earendil-works\pi-coding-agent\dist\bundle\cli.js`，
> 或设置 `THESIS_NODE` 指向 `node.exe`。

## 运行

```powershell
cd <项目目录>
python GUI.py
```

### Agent 助手（推荐）

1. 顶部填写 **kimi API 密钥**，选择论文 **PDF 路径**。
2. （可选）点「Agent 模型」右侧的 **刷新模型**：用当前 API Key 拉取账号可用模型并自动选中较新的 Kimi 模型。默认 `kimi-k2-0905-preview` 若提示 `404 ... Permission denied`，先刷新模型再重试；遇到 `429/quota` 可点 **查询额度** 查看余额，等待约 1 分钟后重试。
3. 切到「Agent 助手（pi）」标签页（默认）：
   - **Agent 提取大纲**：agent 会调用 `thesis_pdf_ingest` 上传 PDF；若返回的 `preview/headings/tail_preview` 已足够则直接生成（省一次模型调用），否则读 `.thesis/pdf_text.txt` → `thesis_outline_save` 落盘 `outline.txt`，完成后自动载入左侧大纲编辑器。
   - **Agent 润色当前段落**：填写「润色位置」和「待润色文字」后点击；agent 先读 `outline.txt`/正文上下文，再保义润色并调用 `thesis_save_polished` 写入 `polished.txt`，完成后自动载入输出面板。
   - **发送**：自由对话（可让 agent 读文件、改大纲、解释某段），支持多轮；`新对话` 清空会话，`停止` 中断当前任务。
4. 状态日志会显示工具调用（`🔧 thesis_pdf_ingest({...})`）、结果（`✅/❌`）、重试与 token 用量。

### 传统流程（兼容）

切到「传统流程（兼容）」标签页，功能与 v1 完全一致：
- **提取论文大纲**：调用 `generate_outline.py`（Moonshot Files API + 1 次 chat 调用），模型取顶部「Agent 模型」。
- **加载/保存大纲**、可编辑大纲可视化。
- **开始润色**：调用 `polish.py` 生成 `polished.txt`（同样支持 `--model`）。

> 低 RPM / 频繁 429 时优先用这个标签页：每个任务只有 1 次模型调用，不会像 agent 多轮工具那样触发 `max RPM` 限制。

> 旧版脚本用 OpenAI SDK 访问 Moonshot OpenAI 兼容接口，需要 `pip install openai`。

### 命令行（旧版，仍可用）

```powershell
set MOONSHOT_API_KEY=your_api_key
python generate_outline.py your_pdf_path
python polish.py --position 需要润色文字所在位置 --text "需要润色文本"
```

## Agent 工具与产物

| 工具 | 作用 | 产物 |
|---|---|---|
| `thesis_pdf_ingest {path, force?}` | 上传 PDF 到 Moonshot Files API 并抽取正文，带 sha1 缓存 | `.thesis/pdf_text.txt`、`.thesis/pdf/<sha1>.txt` |
| `thesis_outline_validate {content}` | 九行模板确定性校验（题目/摘要/第一~五章题目/总结/参考文献） | 校验报告（不写文件） |
| `thesis_outline_save {content}` | 校验并保存大纲，旧文件备份 | `outline.txt`、`outline.txt.bak` |
| `thesis_save_polished {text, position?}` | 保存润色结果，旧文件备份并记录历史 | `polished.txt`、`polished.txt.bak`、`.thesis/polish_history.jsonl` |

Agent 只能使用 `read/grep/ls/find` 四个内置只读工具 + 上述 `thesis_*` 工具（`--tools` 严格白名单），不能随意写文件或执行 shell；所有落盘都由扩展工具完成，带校验/备份/路径约束。

## 配置与环境变量

`.pi/agent/` 是本项目**自带的 pi 配置目录**（通过 `PI_CODING_AGENT_DIR` 指向它，与用户 `~/.pi` 完全隔离，不会污染全局 pi 配置）：

| 文件 | 说明 |
|---|---|
| `.pi/agent/settings.json` | 最小设置：`packages: []`、关闭安装遥测；`retry`（429 自动重试 4 次、5s 起） |
| `.pi/agent/extensions/thesis-tools.ts` | 论文领域工具（TypeScript，pi 用 jiti 直接加载，无需编译） |
| `.pi/prompts/thesis_system.md` | agent 系统提示：角色、绝对规则、九行模板、三类工作流 |

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MOONSHOT_API_KEY` | — | GUI 输入后注入 pi / Python 助手子进程 |
| `THESIS_PROVIDER` | `moonshotai-cn` | 覆盖 pi provider |
| `THESIS_MODEL` | `kimi-k2-0905-preview` | 覆盖模型（pi 0.85.1 目录中还有 `kimi-k2.5`、`kimi-k2.6`、`kimi-k3` 等；GUI 顶部「Agent 模型」下拉框会覆盖此项） |
| `THESIS_PI_CLI` | 自动探测 | pi `cli.js` 绝对路径 |
| `THESIS_NODE` | 自动探测 | `node.exe` 绝对路径 |
| `THESIS_PI_AGENT_DIR` | `<repo>/.pi/agent` | 覆盖 pi 配置目录（测试用临时目录隔离） |
| `THESIS_SYSTEM_PROMPT` | `<repo>/.pi/prompts/thesis_system.md` | 覆盖系统提示文件 |
| `THESIS_PYTHON` | 当前解释器 | 扩展内调用 `tools/pdf_ingest.py` 的解释器 |
| `MOONSHOT_BASE_URL` | `https://api.moonshot.cn/v1` | Files API 地址（测试时指向本地 mock） |

运行时产物（`.thesis/`、`.pi/agent/sessions/`、`auth.json` 等）均已加入 `.gitignore`。

## 测试（离线，不需要真实 API Key）

```powershell
python -m pytest tests -q
```

- `tests/mock_openai_server.py`：同时模拟 OpenAI Chat Completions（SSE 流式 + 工具调用脚本）、Moonshot Files API（upload/content）与账户额度接口（balance）。
- `tests/test_pi_e2e.py`：真实启动 pi，走 JSON 模式与 RPC 主路径，验证 4 个扩展工具真实执行、`outline.txt`/`polished.txt` 真实落盘、`model=` 覆盖真实传给 provider（4 个用例）。
- `tests/test_model_selection.py`：模型列表/账户额度拉取（成功/空/HTTP 错误/网络错误）、模型 404 与 429 中文提示、GUI 刷新模型/自动切换/提交时传递模型/查询额度、rg/fd 依赖准备（16 个用例）。
- `tests/test_agent_runtime.py`：事件解析、CLI 探测、prompt 构建、abort/stop 等（含针对 mock 的 live RPC 用例）。
- `tests/test_thesis_tools.py`：`tools/pdf_ingest.py` 与 mock Files API 的成功/缓存/失败分支、provider 检查、扩展契约与 agent-loop（11 个用例）。
- 无 Node/pi 的环境会自动 skip 端到端用例。

## 目录结构

```text
ThesisPolish/
├─ GUI.py                     # tkinter GUI：Agent 助手 + 传统流程双标签页
├─ agent_runtime.py           # pi RPC 客户端（PiRuntime / 降级 JSON 模式 / prompt 构建）
├─ generate_outline.py        # 旧版：PDF→大纲（OpenAI SDK + Moonshot Files API）
├─ polish.py                  # 旧版：局部润色
├─ outline.txt / polished.txt # 大纲与润色产物（默认路径）
├─ tools/pdf_ingest.py        # PDF → Moonshot Files API → 正文（stdlib + requests）
├─ .pi/
│  ├─ agent/settings.json     # pi 配置（本项目隔离）
│  ├─ agent/extensions/thesis-tools.ts
│  └─ prompts/thesis_system.md
├─ docs/pi-agent-integration.md  # 集成设计说明（架构/契约/事件/Windows 坑/测试策略）
├─ tests/                     # 离线测试 + mock 服务 + fixtures
├─ requirements.txt           # requests（+ 旧版脚本用 openai）
├─ .gitignore
└─ README.md
```

## 故障排查

| 现象 | 处理 |
|---|---|
| Agent 页提示「pi 不可用」/找不到 node | 安装 Node ≥ 22.19 与 `npm i -g @earendil-works/pi-coding-agent`；或设置 `THESIS_NODE` / `THESIS_PI_CLI` |
| 启动报 `No API key found` / HTTP 401 | 检查顶部 API Key 是否为 Moonshot 有效 Key（`pi auth check --provider moonshotai-cn --json` 应为 ready） |
| `Unknown provider "moonshot"` | provider id 是内置的 `moonshotai-cn`（或 `moonshotai`），不是 `moonshot` |
| 模型不存在 / `404 resource_not_found_error: Not found the model ... or Permission denied` | 该模型不在当前 API Key 的可用范围内：在顶部「Agent 模型」点「刷新模型」，选择列表中的模型后重试（GUI 会自动切换）；也可设置 `THESIS_MODEL`。用 `pi --list-models kimi` 可看 pi 内置目录 |
| `429` / `rate limit` / `quota`（如 `max RPM: 3`） | 账户限流或额度不足：点顶部「查询额度」查看余额/代金券；pi 已自动重试 4 次（**20s 起退避**），重试请求同样占用 RPM，**同一分钟内反复重试可能持续被限流**——重启任务时若距上次 429 不足 60 秒，快捷任务会自动等待剩余冷却时间后开始（对话任务会提示剩余秒数）；仍频繁触发说明组织 RPM 配额过低（一次 agent 大纲任务需 3~4 次模型调用），可改用「传统流程（兼容）」单次调用流程，或在 Moonshot 控制台充值/申请提高 RPM |
| 任务日志显示失败，但大纲/润色结果其实已保存 | 新版 GUI 会自动载入本轮已落盘的结果文件并提示「任务实际已完成」；可继续用该文件，或稍后重试收尾步骤 |
| `grep → ripgrep (rg) is not available` | pi 需要 rg/fd：本项目首次启动会自动从全局 pi 配置复制到 `.pi/agent/bin/`（离线无法下载时）；若仍缺失，agent 会用 `read` 分段读取（prompt 已按此优化），不影响主流程 |
| 上传 PDF 很慢/失败 | `thesis_pdf_ingest` 需要访问 api.moonshot.cn；检查网络/代理；`.thesis/pdf/` 缓存可复跑 |
| Agent 只回答但不落盘 | 确认它调用了 `thesis_outline_save` / `thesis_save_polished`；GUI 会提示「未找到大纲文件」而不是载入旧文件 |
| 想临时绕过 pi | 使用「传统流程（兼容）」标签页 |
| 旧版脚本报 `No module named 'openai'` | `python -m pip install -r requirements.txt`（或只使用 Agent 路径，不需要 openai） |
| `pip install -r` 报 `UnicodeDecodeError: 'gbk' codec ...` | 旧版 `requirements.txt` 含中文注释、被 pip 按 GBK 解码失败；当前文件已改为纯 ASCII。若仍报错请确认拉取的是最新文件，或直接 `python -m pip install requests openai` |

## 代码提交说明

- 代码提交到 Openwheel 仓库的 ThesisPolish 目录下。
- 提交信息格式：`ThesisPolish:Added what modules;your_name;date`，例如：

```powershell
git add .
git commit -m "ThesisPolish:integrated pi agent runtime + thesis tools;your_name;2026.10.26"
git push -u origin ThesisPolish
```

## 工作日志 (Work Log)

### 2026-03-30
- 完成了 kimi api 的调用
- 完成了对 pdf 信息的提取，输出到 `outline.txt`
- 完成了对 pdf 输出信息的规范化，符合硕士论文要求
- 增加论文：大纲 + 局部润色功能，输出到 `polish.txt`
### further work
- 整体架构实现了，如何增加交互模块，避免使用命令行（finished）

### 2026-03-31
- 完成了 GUI 界面的实现
### further work
- 完善 GUI 界面功能，增加 kimi 模型选项
- 完善提示词规范性
- 增加更准确的大纲提取（通过对内容解析）
- 学习 tkinter 库
- 增加长文本输入输出功能

### 2026-04-07
- 完成了 GUI 中对大纲的修改和可视化，从而避免大纲识别错误
### further work
- 更加完善提示词的规范性

### 2026-10-26
- 集成 [pi](https://github.com/earendil-works/pi) agent 框架（v0.85.1）：新增 `agent_runtime.py`（RPC 长驻进程 + JSON 降级）、`.pi/agent/extensions/thesis-tools.ts`（4 个论文领域工具）、`.pi/prompts/thesis_system.md`（agent 系统提示）、`tools/pdf_ingest.py`（PDF→Files API→正文，带缓存）
- GUI 重构为「Agent 助手（pi）」+「传统流程（兼容）」双标签页，支持流式输出、工具调用可视化、停止/新对话、结果自动回填
- 新增离线测试体系（mock OpenAI/Files API + 70 个 pytest 用例），全部通过；修复旧版子进程 GBK 编码问题
### further work
- 在线冒烟真实 Kimi API（PDF 上传、长文本、tool calling 稳定性与费用）
- 可选：把 `/thesis-doctor` 这类自检命令与 skill 一并固化，支持在终端直接用 pi 处理论文

# 模板
### 2026-03-31
- 完成了 README.md 文件的编写
### further work
