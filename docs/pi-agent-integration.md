# pi agent 集成说明（ThesisPolish v2）

本文记录 ThesisPolish 如何把 [pi](https://github.com/earendil-works/pi) 作为 agent 运行时嵌入桌面 GUI，以及实现/升级时需要知道的约束。适用于 pi `0.85.1`、Node `24.16`、Windows 11、Python 3.12。

## 1. 为什么选 pi、为什么用 RPC

- pi 是「LLM API + agent loop + 工具/会话」的完整框架，自带 TUI/CLI；本项目只取其**运行时**，界面仍用 Python/tkinter。
- 官方 `docs/sdk.md` 的 RPC Mode Alternative 建议：非 Node 宿主优先用 `pi --mode rpc`，无需 bridge 进程，也避免在 Python 里重造 agent loop。
- 对比三种嵌入方式：

| 方式 | 优点 | 代价 |
|---|---|---|
| `--mode rpc` 长驻（**采用**） | 流式 delta、工具事件、abort、多轮会话、会话切换；无 Windows argv 长度问题 | 需要实现 JSONL 客户端与 request-id 关联 |
| `--mode json` 一次性（降级） | 每请求独立进程，失败隔离简单 | 无多轮/无优雅取消（只能 kill） |
| Node SDK bridge | 类型安全 | 多一个进程 + 自己重造 RPC |

`agent_runtime.py` 同时实现了前两种：`PiRuntime`（主路径）与 `run_once_json()`（降级/脚本化）。

## 2. 运行时数据流

```text
GUI.py
  ├─ 共享输入（key/pdf/outline/position/text）
  ├─ Agent 提取大纲 → build_outline_prompt()
  └─ Agent 润色     → build_polish_prompt()
        │  runtime.ask(prompt, api_key=...)
        ▼
PiRuntime  ── spawn ──> node cli.js --mode rpc --provider moonshotai-cn
        │                     --model kimi-k2-0905-preview
        │                     --no-session --offline
        │                     --tools read,grep,ls,find,thesis_*
        │                     --system-prompt .pi/prompts/thesis_system.md
        │  stdin: {"id":"...","type":"prompt","message":"..."}
        │  stdout: JSONL events（text_delta / toolcall_start|delta|end /
        │          tool_execution_* / agent_settled ...）
        ▼
pi agent loop ── 调扩展工具 ──> .pi/agent/extensions/thesis-tools.ts
                                     ├─ thesis_pdf_ingest → tools/pdf_ingest.py
                                     │     （返回 preview/headings/structure/tail_preview）
                                     ├─ thesis_outline_validate（9 字段 + 章节小标题）
                                     ├─ thesis_outline_save → outline.txt（含缩进小标题）
                                     └─ thesis_save_polished → polished.txt
```

事件在读取线程产生，`PiRuntime` 通过 `on_event` 回调交给 GUI；GUI 只把事件放进 `queue.Queue`，由主线程 `root.after(80ms)` 消费，绝不在回调里碰 tk 控件。

**实时大纲预览**：`_handle_message_update` 会把 `toolcall_start/delta/end` 映射成同名 `AgentEvent`（带 `toolName`/`toolCallId`）。GUI 只对 `thesis_outline_save` / `thesis_save_polished` 做实时预览：累积工具参数 JSON，用容错解析提取当前 `content`/`text` 字段（即使 JSON 尚未闭合），节流刷新到「大纲编辑器 / 润色输出」；同时 GUI 每 600ms 监听 `outline.txt` 变化（agent 或传统流程写盘时立即载入），并用 `_parse_outline_structure` 把“第X章题目 + 两空格缩进小标题”渲染到「章节 / 小标题结构」面板。

## 3. 配置隔离（重要）

- 运行 pi 时设置 `PI_CODING_AGENT_DIR=<repo>/.pi/agent`。该目录同时存放扩展、settings 与 pi 运行时状态（sessions/auth/models-store），因此**不会读写用户 `~/.pi`**，项目行为可复现。
- 生产环境**不需要** `models.json`：pi 0.85.1 内置 `moonshotai-cn`（`https://api.moonshot.cn/v1`，读取 `MOONSHOT_API_KEY`）与 `kimi-k2-0905-preview`（262K ctx）。
- `models.json` 只用于测试时把 provider 的 `baseUrl` 指到本地 mock（放在临时 `PI_CODING_AGENT_DIR` 中；项目 `.pi/models.json` 会被 pi 忽略）。
- API Key 只通过子进程环境变量传入，绝不出现在 argv、日志或仓库文件中。

## 4. 扩展工具契约

`.pi/agent/extensions/thesis-tools.ts` 由 pi 用 jiti 直接加载（无编译步骤），导出 `export default function (pi: ExtensionAPI)`，用 TypeBox 定义参数并 `pi.registerTool(...)`：

- 返回 `{ content: [{type:"text", text}], details: {...} }`；**失败必须 `throw`**（pi 0.85.1 的返回值没有 `isError` 字段，抛错才会让工具结果标记为 `isError:true` 进入模型上下文）。
- 不使用 `ctx.ui.*`：headless（json/print）模式下无 UI，`confirm()` 会立即返回 false。
- 所有写入限制在 `ctx.cwd`（项目根）内；写文件走 `withFileMutationQueue`，带 `.bak` 备份。
- `thesis_pdf_ingest` 通过 `pi` 子进程的 `THESIS_PYTHON`（默认 `python`）调用 `tools/pdf_ingest.py`；正常情况下用管道流式回传进度，若宿主禁止管道（如受限沙箱 EPERM）则回退到「文件重定向 + 同步调用」。返回值新增 `structure: [{title, children[]}]`（章 → 下一级小标题），便于模型在低 RPM 场景下不 read 也能写层级大纲。
- **大纲格式**：9 个固定字段（题目/摘要/第一~五章题目/总结/参考文献）+ 每章行下方两个空格缩进的下一级小标题（如 `  3.1 研究背景`）。`thesis_outline_validate` 会分别校验字段顺序与“小标题只能出现在章节字段之后”，并在 `details.sections/subheadingCount` 返回层级结构；`thesis_outline_save` 原样落盘并返回小标题条数。
- 工具白名单由 `--tools` 控制（`read,grep,ls,find,thesis_pdf_ingest,thesis_outline_validate,thesis_outline_save,thesis_save_polished`），agent 不能执行 shell、不能任意写文件。

## 4.1 模型发现与切换（403/404 处理）

pi 内置模型目录（`pi --list-models kimi`）列出的是 pi 已知的模型，**不代表当前 Moonshot 账号有权限**。账号实际可用性与额度/开通状态相关，常见报错：

```text
404: {"message":"Not found the model kimi-k2-0905-preview or Permission denied",
      "type":"resource_not_found_error"}
```

处理链路：

1. `agent_runtime.fetch_moonshot_models(api_key)`：`GET https://api.moonshot.cn/v1/models`（Bearer Key，可用 `MOONSHOT_BASE_URL` 覆盖地址），返回当前账号可用模型 ID。
2. GUI 顶部「Agent 模型」下拉框 +「刷新模型」按钮：后台线程拉取列表 → 主线程回填；当前模型不在列表时按 `kimi-k3 → kimi-k2.7 → kimi-k2.6 → kimi-k2.5 → kimi-k2 → moonshot-v1-*` 优先级自动挑选。
3. 每次任务把下拉框模型传给 `PiRuntime.ask(..., model=...)`；与运行中进程的配置不同时，runtime 会先 `stop()` 再以新 `--model` 重启（RPC 启动参数不能热改，**会话上下文会清空**）。
4. 任务因 `resource_not_found`/`not found the model` 失败时，GUI 自动刷新模型列表并切换；错误文案由 `humanize_pi_error()` 转成中文指引。
5. 选择持久化在 `.thesis/ui_settings.json`（gitignored）；无 GUI 时可用 `THESIS_MODEL` 环境变量覆盖。

## 4.2 限流（429/quota）与额度诊断

- pi 的 agent 级自动重试在 `.pi/agent/settings.json` 的 `retry` 中配置（本项目：`maxRetries: 4`、`baseDelayMs: 20000`，即 20s/40s/80s/160s 指数退避；必须 ≥ 20s，否则短间隔重试本身会持续占满 60 秒 RPM 窗口）；`retry.provider.maxRetries` 保持 0，避免 SDK 层吞掉限流错误。
- **重试成功的判定**：pi 在失败尝试时会发 `message_end.stopReason="error"` / `auto_retry_start`，重试成功后再发正常 assistant 消息与 `auto_retry_end{success:true}`。`PiRuntime` 会在正常消息/重试成功时清掉 `_last_error`，因此 `agent_settled` → `done.ok=True`；否则会出现「日志写自动重试成功、GUI 却提示任务失败」的误报（已修复并加回归测试）。
- `agent_runtime.fetch_moonshot_balance(api_key)`：`GET {base}/users/me/balance`（Bearer Key），GUI 顶部「查询额度」按钮用它在后台查询余额/代金券，用于区分「限流」与「余额不足」。
- **组织级 RPM 限制**：Moonshot 低等级账号可能返回 `request reached organization max RPM: 3`（每分钟仅 3 次请求）。agent 一次大纲任务至少 3~4 次模型调用（ingest → 可选 read → save → 汇报），因此：
  - prompt 已优化为优先使用 `thesis_pdf_ingest` 返回的 `preview/headings/tail_preview` 直接生成大纲，信息足够时**不调用 `read`**（3 次调用即可完成）；
  - `thesis_outline_save` 自带校验，prompt 不再强制单独调用 `thesis_outline_validate`（少一次调用）；
  - **重试同样计入 RPM**：间隔太短会让 60 秒窗口一直被占满；因此 retry base delay 设为 20s，GUI 在距上次 429 不足 60 秒时：快捷任务**自动等待剩余冷却时间后开始**，对话任务提示剩余秒数；
  - GUI 在「任务失败但结果文件已落盘」时自动载入并提示「任务实际已完成」；`停止` 15 秒未确认会强制 `stop()` pi 进程，避免后台重试风暴；
  - 仍频繁 429 时改用「传统流程（兼容）」：`generate_outline.py` / `polish.py` 每个任务只有 1 次 chat 调用（PDF 正文走 Files API + sha1 缓存，不计入 chat RPM），并支持 `--model`（GUI 传当前模型）。这两个脚本**不再依赖 openai SDK**，改用 `tools/moonshot_chat.py`（requests 直连）——openai 3.x（依赖 httpx2）在部分 Windows/Python 环境会报 `TypeError: process() takes no keyword arguments`，绕开可稳定运行；
  - 或在 Moonshot 控制台充值/申请提高组织 RPM 配额。
  - **升级 RPM 后仍 429 的排查**：① 旧 GUI 进程不会自动热更新代码，必须重启（徽标含 `v2.5` 才是新代码），并等 60 秒清空窗口；② GUI 顶部「诊断限流」运行 `tools/moonshot_doctor.py`，输出可用模型、余额、最小 chat 的 HTTP 状态/完整 429 原文/限流响应头，当前模型失败时会自动测试其它可用模型并给出替代建议；③ 限流可能分层：组织（org-…）/项目（proj-…）/单模型，组织 RPM 升级不等于项目 RPM 生效，需在控制台确认项目配额；④ 用诊断脚本逐个模型测试，排除单模型限制；⑤ 实时确认余额（¥ 余额足够不代表 RPM 生效）。
- **temperature 兼容**：Kimi K2.5+/K2.6/K2.7/K3 系列只允许 `temperature=1`（传其它值返回 400 `invalid temperature`）。`tools/moonshot_chat.py` 对这类模型不发送 `temperature` 字段（用服务端默认），并在收到 400 invalid temperature 时自动去掉该字段重试一次；pi agent 路径本身不传 temperature，不受影响。
- 429 的错误文案会附带原始错误片段（含 RPM 数字时给出专门提示），并提示查额度/等待/换模型/传统流程兜底。
- 降低 token 消耗：`thesis_pdf_ingest` 的 `preview=3000` 字符、`tail_preview=1500` 字符，覆盖题目/摘要与结论/参考文献；`grep` 只作可选优化，不可用时不影响主流程。

## 4.3 grep/find 依赖（rg/fd）

pi 的 `grep`/`find` 工具依赖 ripgrep（rg）与 fd：优先使用 `<PI_CODING_AGENT_DIR>/bin`，其次 PATH，最后联网下载。本项目 `--offline` 禁止下载，因此 `PiRuntime.start()` 首次会尝试把本机全局 pi 配置（`~/.pi/agent/bin`）里的 `rg`/`fd` 复制到 `.pi/agent/bin/`（已 gitignore）。三端都没有时 grep/find 会报不可用，此时 agent 按 prompt 指引用 `read` 分段读取，不影响大纲/润色主流程。

## 5. 事件与完成语义

- JSON 模式：首行 `{"type":"session",...}`，随后 `agent_start/turn_start/message_start/message_update/tool_execution_*/turn_end/agent_end/agent_settled`；`message_update.assistantMessageEvent` 为 delta-only（`text_delta` 带 `contentIndex`）。
- 最终答案：取 `agent_end.messages` 中最后一条 `role=="assistant"` 的 text（`PiRuntime.last_assistant_text`）。
- LLM/API 失败时进程退出码仍可能为 0：必须解析 `message_end.stopReason=="error"` 与 `auto_retry_*` 事件；provider/密钥类启动错误则是 `rc=1 + stderr` 文本。**注意重试成功会覆盖此前错误**：只有在最终 `agent_settled` 时仍存在未清除的错误，`done.ok` 才应为 False。
- RPC 模式下命令带 `id`，`response` 回带相同 `id`；事件与 response 会交错，客户端必须逐行分发、不能「等到事件结束再读 response」。

## 6. Windows 注意事项（已在 0.85.1 实测）

1. **命令行长度**：Windows argv 约 32K 字符上限。论文正文/长 prompt 必须走 RPC 的 stdin JSON 行或 JSON 模式的 stdin 合并，不能放 argv。
2. **编码**：pi 的 JSON/RPC 输出为 UTF-8；Python 侧统一 `encoding="utf-8", errors="replace"`，不要用 `locale.getpreferredencoding()`（中文 Windows 为 GBK）。
3. **关闭流程**：执行过工具/扩展开窗后立刻关闭 RPC stdin，可能触发 Node/libuv 断言崩溃（exit 3221226505）。`PiRuntime.stop()` 先尽力 `abort`，再 `terminate()/kill()`，不依赖正常退出码。
4. **启动方式**：优先用绝对路径 `node.exe` + `dist/bundle/cli.js`（避开 `.cmd` 经 cmd.exe 时的 `%VAR%` 展开）；加 `CREATE_NO_WINDOW` 不弹黑窗。
5. `.pi/agent` 下由 pi 生成的 `auth.json`、`models-store.json`、`sessions/` 均已 gitignore。

## 7. 离线测试策略

- `tests/mock_openai_server.py` 同时实现 OpenAI 兼容的 `/v1/chat/completions`（SSE、可脚本化多步工具调用）与 Moonshot Files API（`/v1/files`、`/v1/files/{id}/content`）。
- 测试把仓库里的 `.pi/agent/extensions/thesis-tools.ts`、`.pi/prompts/thesis_system.md`、`tools/pdf_ingest.py` 复制到临时项目，写一个指向 mock 的临时 `models.json`，然后真实启动 `pi`，断言：工具执行事件、`outline.txt`/`polished.txt`/`.thesis/pdf_text.txt` 落盘、RPC 主路径的流式文本与 done 事件。
- 无 Node/pi 的环境自动 skip 端到端用例；全部用例不访问外网、不需要真实 API Key。

```powershell
python -m pytest tests -q
```

## 8. 版本与升级风险

- 账号额度/限流（429）按 §4.2 诊断；真实限额与计费以 Moonshot 控制台为准，kimi-k2.6 等大模型单位 token 成本/限额更高，长文本任务优先保证余额。
- 项目按 pi `0.85.1` 的 CLI/事件/扩展 API 验证；升级 pi 后请先跑离线测试，再在线冒烟真实 Kimi（PDF 上传、长文本、tool calling 稳定性与计费）。
- 模型目录随 pi 版本变化（0.85.1 已含 `kimi-k2.5/kimi-k2.6/kimi-k3` 等）；可用 `THESIS_MODEL`/`THESIS_PROVIDER` 覆盖，或 `pi --list-models kimi` 查看当前目录。
- 若未来 pi 内置 provider 移除目标模型，可在 `PI_CODING_AGENT_DIR/models.json` 中补充自定义模型条目（格式见 pi `docs/models.md`）。

## 9. v3 现代 UI 设计体系（`ui_theme.py`）

GUI 从「默认 ttk 控件」升级为自带设计语言的桌面界面，全部集中在 `ui_theme.py`：

- **设计 token**：`PALETTES["dark"|"light"]` 定义背景/表面/卡片/描边/正文/次要文字/主色（靛蓝 `#6366F1`）/成功/警告/错误/输入框/控制台/滚动条等颜色；字号沿用 8pt 网格间距。
- **字体**：界面优先 `Segoe UI Variable Display` → `Microsoft YaHei UI` → `Segoe UI`；日志/代码用 `Cascadia Mono` → `Consolas`；高 DPI 通过 `enable_dpi_awareness()`（Per-Monitor V2）在创建窗口前开启。
- **ttk 样式**：`configure_ttk()` 统一配置按钮（`Primary/Secondary/Ghost/Danger/Small` 五级）、输入框、下拉框、滚动条、隐藏页签的 Notebook、Panedwindow 等；`clam` 作为基底，避免依赖额外主题包。
- **主题化控件**：`Card`（描边卡片 + 标题/提示 + body）、`Pill`（圆角状态胶囊）、`NavItem`（自定义导航，选中态主色实心）、`ThemedLabel`、`ThemedText`（日志/编辑器，标签配色随主题）、`BorderedText`、`Brand` 等；所有控件实现 `apply_palette(palette)` 并注册到 `Theme`，切换主题时 `Theme.apply_all()` 一次性换肤。
- **布局**：顶部品牌栏（标题 + 副标题自然撑开，避免高 DPI 裁切；状态胶囊 + 主题切换）+ 左侧「运行配置 / 工作区」侧边栏（`ttk.Panedwindow` 可拖拽调整宽度，宽度持久化到 `ui_settings.json`）+ 自定义导航（Agent 助手 / 传统流程，`tkraise` 切换，不使用 Notebook 页签行）+ 卡片式内容区 + 底部状态栏；页面切换/状态胶囊/密钥状态均为自定义控件。
- **持久化**：`.thesis/ui_settings.json` 同时保存 `model` 与 `theme`（深/浅），右上角 `◐` 按钮即时切换，无需重启。
- **运行时换肤兜底**：`remap_tk_colors()` 会遍历界面中未注册到 `Theme` 的普通 tk 容器（`tk.Frame` / `tk.Label` 等），把旧调色板颜色按 key 映射为新主题颜色；否则运行时切浅色时这些临时容器会残留深色背景块。
- **截图**：`docs/screenshots/gui_v3_dark.png`、`gui_v3_light.png`；视觉回归可人工检查这两张图。
