# ThesisPolish

> 面向硕士毕业论文的 **大纲提取 + 局部保义润色** 桌面工具，由本地 [pi](https://github.com/earendil-works/pi) Agent 驱动；生成过程实时显示在 GUI 中。

![版本](https://img.shields.io/badge/version-v2.6_(2026--10--27)-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.10%2B-yellow)
![Node](https://img.shields.io/badge/node-%E2%89%A522.19-brightgreen)

- **平台**：Windows 10/11（主要验证环境）
- **模型**：Moonshot / Kimi（OpenAI 兼容 API，Key 只在本地使用、不落盘）
- **运行时**：Python 3.12 + tkinter、Node.js ≥ 22.19、pi 0.85.1
- **许可**：MIT（见 [LICENSE](LICENSE)）

---

## 目录

- [项目简介](#项目简介)
- [功能特性](#功能特性)
- [界面](#界面)
- [系统架构](#系统架构)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [大纲格式](#大纲格式)
- [命令行（单次调用）](#命令行单次调用)
- [配置与环境变量](#配置与环境变量)
- [目录结构](#目录结构)
- [测试](#测试)
- [常见问题](#常见问题)
- [更新记录](#更新记录)
- [贡献与提交约定](#贡献与提交约定)
- [许可](#许可)

## 项目简介

ThesisPolish 面向硕士毕业论文场景：把论文 PDF 变成**层级大纲**（9 个固定字段 + 每章下一级小标题），再按大纲对局部段落做**保持原意**的学术化润色，避免传统"逐段复制粘贴给聊天模型"造成的逻辑割裂与上下文丢失。

v2 起引入 [pi](https://github.com/earendil-works/pi) agent 框架：润色/提大纲不再是一段固定 prompt 的单次调用，而是由本地 pi agent 自主编排——上传 PDF、阅读正文、生成并校验大纲、调用工具落盘；GUI 实时显示流式输出、工具调用、**正在生成的大纲内容**与**章节小标题结构**。同时保留"传统流程"（单次模型调用），在低 RPM / 不限流环境受限时作为兜底。

## 功能特性

- **Agent 驱动（pi）**：Agent 助手标签页支持「提取大纲」「润色当前段落」一键任务与自由多轮对话；工具调用、重试、token 用量、错误原因实时可见。
- **实时大纲预览**：模型流式生成 `thesis_outline_save` 参数时，大纲编辑器逐字刷新；`outline.txt` 一落盘立即载入。
- **章节 / 小标题结构面板**：实时显示每章识别到的**下一级小标题**（如 `3.1`、`3.2`），并统计「共 N 章 / M 条小标题」。
- **层级大纲格式**：9 个固定字段 + 每章行下方两个空格缩进的下一级小标题，`thesis_outline_validate` 做确定性校验。
- **模型可切换**：顶部「Agent 模型」用当前 Key 拉取账号可用模型列表（`GET /v1/models`），404 时自动刷新并切换；选择持久化到 `.thesis/ui_settings.json`。
- **额度与限流处理**：顶部「查询额度」「诊断限流」；429 自动重试（4 次、20s 起指数退避）、冷却自动等待、重试成功不再误报失败、结果已落盘时自动恢复；兼容 Kimi K2.5+/K3 "只允许 temperature=1" 的限制。
- **长文本安全**：prompt 走 RPC JSON 行 / stdin，绝不放进 Windows 命令行（约 32K 字符上限）。
- **可取消、可多轮**：`停止` 发送 abort（15s 未确认强制结束进程）；`新对话` 新建 pi 会话。
- **双轨兼容**：传统流程只依赖 `requests`（不再依赖 openai SDK），每个任务仅 1 次模型调用，低 RPM 账号首选。
- **离线可验收**：内置 OpenAI / Moonshot Files API / 余额接口 mock 服务，115 个 pytest 用例，无需真实 API Key 即可跑通完整链路。

## 界面

![界面截图（v1）](picture/GUI.JPG)

> 上图为 v1 界面截图；v2 已改为「Agent 助手（pi）」+「传统流程（兼容）」双标签页，并在 Agent 页新增流式输出、实时大纲编辑器与章节小标题结构面板。

## 系统架构

```text
GUI.py (tkinter)
├─ 共享头部：API Key / Agent 模型[刷新] / 额度[查询] / 限流诊断 / PDF / 路径
├─ Tab 1「Agent 助手（pi）」 ← 默认
│    状态日志 · 流式输出 · 章节/小标题结构（实时） · 大纲编辑器（实时刷新）
└─ Tab 2「传统流程（兼容）」 → generate_outline.py / polish.py（单次调用）
        │ on_event 回调只入 queue，主线程 root.after(80ms) 轮询
        ▼
agent_runtime.py（PiRuntime，标准库）
        │ 主路径：node <pi-cli.js> --mode rpc（长驻、流式、abort、多轮）
        │ 降级：--mode json 一次性进程（正文经 stdin，无 argv 长度限制）
        ▼
pi 0.85.1（Node）── provider: moonshotai-cn / 可切换 Kimi 模型
        │ 工具白名单：read, grep, ls, find + thesis_*
        ▼
.pi/agent/extensions/thesis-tools.ts（TypeScript，pi 直接加载，无需编译）
     ├─ thesis_pdf_ingest        → tools/pdf_ingest.py → Moonshot Files API
     │     返回 preview / headings / structure / tail_preview（带 sha1 缓存）
     ├─ thesis_outline_validate  → 9 字段 + 章节小标题确定性校验
     ├─ thesis_outline_save      → outline.txt（校验 + .bak 备份）
     └─ thesis_save_polished     → polished.txt（.bak + 润色历史 JSONL）
```

- GUI 与 pi 之间是**严格 JSONL 协议**（RPC）：命令带 `id`、事件带类型；`toolcall_start/delta/end` 被转成 GUI 事件，用于实时预览工具参数中的 `content`。
- 详细设计（事件语义、扩展 API、Windows 坑、测试策略、限流/RPM 处理）见 [docs/pi-agent-integration.md](docs/pi-agent-integration.md)。

## 环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10/11 | 主要开发与验证环境 |
| Python | 3.10+（实测 3.12） | tkinter、requests |
| Node.js | ≥ 22.19（实测 24.16） | pi 运行时 |
| pi | 0.85.1（锁定验证） | `npm i -g @earendil-works/pi-coding-agent` |
| 网络 | 可访问 `https://api.moonshot.cn` | 仅调用 Kimi API 时需要 |
| API Key | Moonshot / Kimi Key | 在 GUI 顶部输入，只注入子进程环境变量 |

## 安装

```powershell
# 1) Python 依赖（PDF 工具 / 传统流程使用；pi 运行时本身不需要 Python 包）
python -m pip install -r requirements.txt

# 2) Node 与 pi（已安装可跳过；建议固定 0.85.x）
node --version                                    # >= 22.19
npm i -g @earendil-works/pi-coding-agent
pi --version                                      # 期望 0.85.1

# 3) 验证 pi 内置 Moonshot provider
$env:MOONSHOT_API_KEY = "sk-..."
pi auth check --provider moonshotai-cn --json     # {"status":"ready", ...}
```

> `pi` 不在 PATH 时，可设置 `THESIS_PI_CLI` 指向
> `...\node_modules\@earendil-works\pi-coding-agent\dist\bundle\cli.js`，
> 或设置 `THESIS_NODE` 指向 `node.exe`。

## 快速开始

```powershell
python GUI.py
```

1. 顶部填入 **kimi API 密钥**，选择论文 **PDF 路径**；
2. （可选）点「**刷新模型**」：用当前 Key 拉取账号可用模型并自动选中；若提示 `404 ... Permission denied` 先刷新，再重试；
3. 进入「**Agent 助手（pi）**」标签页：
   - **Agent 提取大纲**：agent 上传 PDF → 读取正文 → 生成层级大纲 → 校验 → 写入 `outline.txt`；生成过程中大纲编辑器与「章节 / 小标题结构」面板**实时刷新**，完成自动载入编辑器；
   - **Agent 润色当前段落**：填写「润色位置」与「待润色文字」，agent 读大纲/正文上下文后保义润色，写入 `polished.txt` 并载入输出面板；
   - **发送**：自由对话（可让 agent 读文件、改大纲、解释某段）；`新对话` 清空会话，`停止` 中止任务；
4. 「**传统流程（兼容）**」标签页保留单次调用流程（`generate_outline.py` / `polish.py`），模型沿用顶部「Agent 模型」，适合低 RPM 或 pi 不可用时。

> 遇到 `429/quota`：点「**查询额度**」确认余额、点「**诊断限流**」查看完整错误与限流响应头；pi 会自动退避重试，GUI 也会在冷却期内自动等待后再开始。

## 大纲格式

`outline.txt` = 9 个固定字段 + 每章行下方两个空格缩进的**下一级小标题**（纯文本，可直接在 GUI 编辑器里手改）：

```text
题目：REALM: An MLLM-Agent Framework ...
摘要：...
第一章题目：Introduction
  1.1 Motivation
  1.2 Contributions
第二章题目：Related Works
  2.1 3D Scene Representations
  2.2 3D Open-World Understanding
第三章题目：Methodology
  3.1 3D Feature Field for Reasoning
第四章题目：Experiments
  4.1 Experimental Settings
第五章题目：Conclusion
总结：...
参考文献：有
```

规则：

- 9 个字段必须齐全、顺序固定（缺失写“未提供”）；
- 小标题只出现在“第X章题目”行下方，**两个空格缩进**、保留原文编号，每章只取直接下一级、最多 8 条；
- 正文没有的小标题省略、**不要虚构**；小标题缺失只警告，出现在章节字段之外会报错；
- 校验/保存通过 `thesis_outline_validate`、`thesis_outline_save`，保存时旧文件备份为 `outline.txt.bak`。

## 命令行（单次调用）

不需要启动 GUI，也没有 pi/Node 依赖：

```powershell
$env:MOONSHOT_API_KEY = "sk-..."
python generate_outline.py 论文.pdf --model kimi-k2.6
python polish.py --position "第三章 3.2" --text "待润色文本" --model kimi-k2.6
```

- 每个任务只有 **1 次 chat 调用**（PDF 正文走 Files API + sha1 缓存，不占 chat RPM），低 RPM 账号首选；
- 不依赖 openai SDK（直接使用 `requests`），避免 openai 3.x/httpx2 的解压兼容问题。

## 配置与环境变量

`.pi/agent/` 是本项目**自带的 pi 配置目录**（通过 `PI_CODING_AGENT_DIR` 指向，与用户 `~/.pi` 完全隔离）：

| 路径 | 说明 |
|---|---|
| `.pi/agent/settings.json` | 最小设置：`packages: []`、关闭安装遥测；`retry`（429 重试 4 次、20s 起） |
| `.pi/agent/extensions/thesis-tools.ts` | 4 个论文领域工具（TypeScript，pi 用 jiti 直接加载） |
| `.pi/prompts/thesis_system.md` | agent 系统提示：角色、规则、大纲格式、工作流 |

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MOONSHOT_API_KEY` | — | GUI 输入后注入 pi 与 Python 助手子进程 |
| `THESIS_PROVIDER` | `moonshotai-cn` | 覆盖 pi provider |
| `THESIS_MODEL` | `kimi-k2-0905-preview` | 覆盖模型；GUI 顶部下拉框优先 |
| `THESIS_PI_CLI` | 自动探测 | pi `cli.js` 绝对路径 |
| `THESIS_NODE` | 自动探测 | `node.exe` 绝对路径 |
| `THESIS_PI_AGENT_DIR` | `<repo>/.pi/agent` | 覆盖 pi 配置目录（测试用临时目录隔离） |
| `THESIS_SYSTEM_PROMPT` | `<repo>/.pi/prompts/thesis_system.md` | 覆盖系统提示文件 |
| `THESIS_PYTHON` | 当前解释器 | 扩展内调用 `tools/pdf_ingest.py` 的解释器 |
| `MOONSHOT_BASE_URL` | `https://api.moonshot.cn/v1` | API 地址（测试时指向本地 mock） |

运行时产物（`.thesis/`、`.pi/agent/bin/`、`auth.json`、`sessions/` 等）均已 gitignore。

## 目录结构

```text
ThesisPolish/
├─ GUI.py                     # tkinter GUI：Agent 助手 + 传统流程双标签页
├─ agent_runtime.py           # pi RPC 客户端（PiRuntime / JSON 降级 / prompt 构建）
├─ generate_outline.py        # 传统流程：PDF→大纲（Files API + 1 次 chat，requests）
├─ polish.py                  # 传统流程：局部润色（1 次 chat，requests）
├─ outline.txt / polished.txt # 大纲与润色产物（演示文件；运行会覆盖）
├─ .pi/
│  ├─ agent/settings.json     # pi 配置（本项目隔离）
│  ├─ agent/extensions/thesis-tools.ts
│  └─ prompts/thesis_system.md
├─ tools/
│  ├─ pdf_ingest.py           # PDF → Moonshot Files API → 正文/标题/结构（sha1 缓存）
│  ├─ moonshot_chat.py        # chat 轻量客户端（requests，temperature 兼容）
│  └─ moonshot_doctor.py      # 限流/额度/模型权限诊断
├─ docs/pi-agent-integration.md  # 集成设计说明（架构/契约/事件/Windows/限流）
├─ tests/                     # 离线测试 + mock 服务 + fixtures
├─ requirements.txt
├─ LICENSE
└─ README.md
```

## 测试

```powershell
python -m pytest tests -q        # 当前：115 passed（离线，无需真实 API Key）
```

- `tests/mock_openai_server.py`：模拟 OpenAI Chat Completions（SSE + 可脚本化工具调用）、Moonshot Files API、账户余额接口；
- `tests/test_pi_e2e.py`：真实启动 pi（JSON 模式与 RPC），验证工具执行、`outline.txt`/`polished.txt` 落盘、模型覆盖、章节小标题校验/保存；
- `tests/test_model_selection.py`：模型/额度/诊断、GUI 实时预览、小标题结构面板、文件监听、冷却等待等；
- `tests/test_agent_runtime.py`：事件解析（含 `toolcall_*` 流式）、CLI 探测、prompt、abort/stop；
- `tests/test_thesis_tools.py`：PDF 工具、`structure` 层级、扩展契约与 agent-loop；
- `tests/test_legacy_scripts.py`：传统流程单次调用、429/5xx 重试、temperature 兼容、诊断脚本。

无 Node/pi 的环境会自动跳过端到端用例。

## 常见问题

| 现象 | 处理 |
|---|---|
| Agent 页提示「pi 不可用」/找不到 node | 安装 Node ≥ 22.19 与 `npm i -g @earendil-works/pi-coding-agent`；或设置 `THESIS_NODE` / `THESIS_PI_CLI` |
| `No API key found` / 401 | 检查顶部 Key 是否为 Moonshot 有效 Key（`pi auth check --provider moonshotai-cn --json`） |
| `404 resource_not_found_error: Not found the model ...` | 该模型不在当前账号可用范围：点「刷新模型」选择列表中的模型；可用 `pi --list-models kimi` 查看 pi 目录 |
| `invalid temperature: only 1 is allowed` | Kimi K2.5+/K3 系列只允许 `temperature=1`；新版旧版脚本/诊断已自动省略 temperature 并自动重试，更新到 v2.5+ 即可 |
| `429/quota`（含 `organization max RPM`） | 点「查询额度」查余额；pi 已自动重试（20s 起退避），重试同样占 RPM——冷却期内 GUI 会自动等待；仍频繁触发说明组织/项目 RPM 过低，用「传统流程」单次调用或到控制台充值/申请提高 RPM |
| 提高 RPM 后仍 429 | 确认徽标是**新版本**（旧 GUI 进程需重启）、等 60 秒；点「诊断限流」看完整错误与限流响应头（限流可能分组织/项目/单模型层） |
| 诊断脚本最小 chat 返回 400 `temperature` | 旧代码问题；更新后诊断不传 temperature，会自动测试其它可用模型并给出替代建议 |
| `grep → ripgrep (rg) is not available` | pi 的 grep/find 依赖 rg/fd：首次启动会从全局 pi 配置复制到 `.pi/agent/bin/`；缺失时 agent 改用 `read`，不影响主流程 |
| `pip install -r` 报 `UnicodeDecodeError: 'gbk'` | requirements.txt 已改为纯 ASCII；若仍报错可 `python -m pip install requests` |
| 任务失败但大纲/润色文件已生成 | 新版会自动载入本轮已落盘结果并提示「任务实际已完成」；可继续编辑/重试收尾 |
| 想临时绕过 pi | 使用「传统流程（兼容）」标签页 |
| 关闭后仍有 python/node 残留 | 新版「停止」15s 未确认会强制结束 pi；仍异常时在任务管理器结束残留进程后重启 |

## 更新记录

| 版本 | 时间 | 内容 |
|---|---|---|
| v1 | 2026-03 ~ 04 | Kimi API 调用、PDF 大纲提取、GUI、大纲可视化编辑、局部润色 |
| v2.0 | 2026-10-26 | 引入 pi agent：`agent_runtime.py`、4 个扩展工具、Agent 标签页、离线 mock 测试体系 |
| v2.3 ~ v2.5 | 2026-10-27 | 模型发现/切换、额度与限流诊断、429 重试与冷却修复、旧版脚本去 openai SDK（requests）、Kimi 固定 temperature 兼容 |
| v2.6 | 2026-10-27 | **大纲实时预览**、**章节/小标题结构面板**、层级大纲（9 字段 + 每章下一级小标题）、文件实时监听 |

## 贡献与提交约定

- 代码提交到本仓库 `ThesisPolish` 分支/目录；
- 提交信息格式：`ThesisPolish:Added what modules;your_name;date`，例如：

```powershell
git add .
git commit -m "ThesisPolish:added live outline preview;your_name;2026.10.27"
git push -u origin main
```

- 提交前请先跑离线测试：`python -m pytest tests -q`；
- 请勿提交个人论文 PDF、`.thesis/` 缓存、API Key（均已加入 `.gitignore`）。

## 许可

本项目基于 **MIT License** 开源，详见 [LICENSE](LICENSE)。

Copyright (c) 2026 rrdongdong
