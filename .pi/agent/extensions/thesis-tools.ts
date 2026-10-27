/**
 * ThesisPolish 项目本地 pi 扩展（pi 0.85.1）。
 *
 * 仓库路径：  .pi/agent/extensions/thesis-tools.ts
 * 加载方式：  pi 通过 jiti 直接加载，无需编译；配合 PI_CODING_AGENT_DIR=<仓库>/.pi/agent
 *            时按 agent 目录的 extensions/*.ts 自动发现（无需 --approve、无需 -e）。
 *            也可显式：pi -e .pi/agent/extensions/thesis-tools.ts
 *
 * 工具（名称是 Python 运行时 / GUI 的 API 契约，禁止随意改名）：
 *   thesis_pdf_ingest       调 tools/pdf_ingest.py：上传 PDF → 导出 .thesis/pdf_text.txt，
 *                           返回 JSON 摘要；失败抛错（pi 会标记 isError=true）。
 *   thesis_outline_validate 确定性校验九行大纲模板（题目/摘要/第一章..第五章题目/总结/参考文献）。
 *   thesis_outline_save     校验 + 备份 outline.txt → outline.txt.bak + UTF-8 原子写入。
 *   thesis_save_polished    备份并写入 polished.txt，追加 .thesis/polish_history.jsonl。
 *
 * 设计约束：
 *   - 无 ctx.ui 对话框（--mode json / rpc / print 均为 headless，安全）。
 *   - 所有写盘路径固定在项目根目录（ctx.cwd）内；PDF 输入只读，允许在项目外。
 *   - 子进程优先用管道 stdio（正常 GUI 环境可实时读进度）；若沙箱拒绝管道（EPERM，
 *     DSH 环境实测），退回 spawnSync + 文件重定向，保证离线可验收。
 *   - 失败一律 throw Error（pi 扩展 API 没有 isError 返回字段；throw 才会把
 *     tool result 标记为 isError:true 并报告给模型）。
 */
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { withFileMutationQueue } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { spawn, spawnSync } from "node:child_process";
import { appendFile, copyFile, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { closeSync, openSync, readFileSync, unlinkSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";
import { tmpdir } from "node:os";

// ---------------------------------------------------------------------------
// 九行大纲模板（与 generate_outline.py / README 的模板一致；注意是 9 行不是 7 行）
// ---------------------------------------------------------------------------
const OUTLINE_LABELS = [
  "题目",
  "摘要",
  "第一章题目",
  "第二章题目",
  "第三章题目",
  "第四章题目",
  "第五章题目",
  "总结",
  "参考文献",
] as const;
type OutlineLabel = (typeof OUTLINE_LABELS)[number];

interface OutlineReport {
  valid: boolean;
  errors: string[];
  warnings: string[];
  fields: Partial<Record<OutlineLabel, string>>;
  missing: string[];
  lineCount: number;
}

const short = (text: string, max = 52): string =>
  text.length <= max ? text : `${text.slice(0, max)}...`;

function validateOutline(raw: string): OutlineReport {
  const errors: string[] = [];
  const warnings: string[] = [];
  const fields: Partial<Record<OutlineLabel, string>> = {};
  const seen: OutlineLabel[] = [];
  const labelRe = new RegExp(`^(${OUTLINE_LABELS.join("|")})[：:](.*)$`);
  const lines = raw
    .replace(/^\uFEFF/, "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((text, index) => ({ text: text.trim(), no: index + 1 }))
    .filter((entry) => entry.text.length > 0);

  for (const { text, no } of lines) {
    const match = text.match(labelRe);
    if (!match) {
      errors.push(`第 ${no} 行不是模板字段（模板外内容一律不允许）: ${short(text)}`);
      continue;
    }
    const label = match[1] as OutlineLabel;
    const value = match[2].trim();
    if (!text.includes("：")) warnings.push(`第 ${no} 行使用了半角冒号，建议改为全角“：”`);
    if (seen.includes(label)) {
      errors.push(`字段重复: ${label}`);
      continue;
    }
    seen.push(label);
    fields[label] = value;
    if (!value) errors.push(`字段为空: ${label}（缺失内容请填“未提供”）`);
    else if (/^<.*>$/.test(value)) warnings.push(`字段 ${label} 疑似仍是模板占位符: ${short(value)}`);
  }
  for (const label of OUTLINE_LABELS) if (!(label in fields)) errors.push(`缺少字段: ${label}`);
  const ordered = OUTLINE_LABELS.filter((label) => seen.includes(label));
  if (ordered.join() !== seen.join()) errors.push(`字段顺序错误，必须为: ${OUTLINE_LABELS.join(" / ")}`);
  if (seen.length === 0) errors.push("内容为空或没有任何模板字段");
  return {
    valid: errors.length === 0,
    errors,
    warnings,
    fields,
    missing: OUTLINE_LABELS.filter((label) => !(label in fields)),
    lineCount: lines.length,
  };
}

function formatReport(report: OutlineReport): string {
  const lines = [`模板校验: ${report.valid ? "通过" : "未通过"}（字段行 ${report.lineCount}/${OUTLINE_LABELS.length}）`];
  for (const error of report.errors) lines.push(`  ✗ ${error}`);
  for (const warning of report.warnings) lines.push(`  ! ${warning}`);
  if (report.valid) lines.push("  ✓ 9 个字段齐全、顺序正确、无空值");
  return lines.join("\n");
}

/** 统一换行 / 去 BOM / 末尾补一个换行。 */
function normalizeFileText(raw: string): string {
  const body = raw.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").trim();
  return body.length > 0 ? `${body}\n` : "";
}

// ---------------------------------------------------------------------------
// 路径 / 文件系统辅助
// ---------------------------------------------------------------------------
/** 项目根目录：pi 进程的 cwd（Python 运行时启动 pi 时固定为仓库根目录）。 */
function projectRoot(ctx: ExtensionContext): string {
  return resolve(ctx.cwd);
}

/** 展示用相对路径（尽量短、统一正斜杠）。 */
function displayPath(root: string, absolute: string): string {
  const relativePath = absolute.startsWith(root) ? absolute.slice(root.length).replace(/^[\\/]+/, "") : "";
  return relativePath.length > 0 ? relativePath.replaceAll("\\", "/") : absolute;
}

/** 去掉模型偶尔加在路径前的 @ 前缀（与内置工具行为一致）。 */
function stripAtPrefix(input: string): string {
  const trimmed = input.trim();
  return trimmed.startsWith("@") ? trimmed.slice(1) : trimmed;
}

async function exists(path: string): Promise<boolean> {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

async function backupIfExists(path: string): Promise<string | undefined> {
  if (!(await exists(path))) return undefined;
  await copyFile(path, `${path}.bak`);
  return `${path}.bak`;
}

// ---------------------------------------------------------------------------
// 子进程：优先管道 stdio；沙箱 EPERM 时退回「文件重定向」方式
// ---------------------------------------------------------------------------
interface RunResult {
  stdout: string;
  stderr: string;
  code: number;
  via: "pipe" | "file";
}

const RUN_TIMEOUT_MS = 300_000;

class SpawnBlockedError extends Error {}

function isEperm(error: unknown): boolean {
  const code = (error as { code?: string } | null)?.code;
  const message = String((error as Error | null)?.message ?? error);
  return code === "EPERM" || /EPERM|operation not permitted/i.test(message);
}

function runWithPipes(
  command: string,
  args: string[],
  cwd: string,
  timeoutMs: number,
  signal: AbortSignal | undefined,
  onStderrLine: ((line: string) => void) | undefined,
): Promise<RunResult> {
  return new Promise<RunResult>((resolvePromise, rejectPromise) => {
    let settled = false;
    let stdout = "";
    let stderr = "";
    const finish = (action: () => void) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      action();
    };

    let child;
    try {
      child = spawn(command, args, {
        cwd,
        env: process.env,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      rejectPromise(new SpawnBlockedError(String(error)));
      return;
    }

    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        /* ignore */
      }
      finish(() => rejectPromise(new Error(`Python 处理超时（>${Math.round(timeoutMs / 1000)} 秒）`)));
    }, timeoutMs);

    const onAbort = () => {
      try {
        child.kill();
      } catch {
        /* ignore */
      }
    };
    signal?.addEventListener("abort", onAbort, { once: true });

    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
    });
    let partialStderr = "";
    child.stderr?.on("data", (chunk: Buffer) => {
      const text = chunk.toString("utf8");
      stderr = (stderr + text).slice(-16_000);
      partialStderr += text;
      const lines = partialStderr.split(/\r?\n/);
      partialStderr = lines.pop() ?? "";
      for (const line of lines) {
        if (line.trim().length > 0) onStderrLine?.(line.trim());
      }
    });
    child.on("error", (error) => {
      if (isEperm(error)) finish(() => rejectPromise(new SpawnBlockedError(String(error))));
      else finish(() => rejectPromise(error));
    });
    child.on("close", (code, closeSignal) => {
      finish(() =>
        resolvePromise({
          stdout,
          stderr,
          code: typeof code === "number" ? code : closeSignal ? 1 : 1,
          via: "pipe",
        }),
      );
    });
  });
}

/** 文件重定向方式：不创建管道，DSH 等沙箱下实测可用（stdout/stderr 落临时文件）。 */
function runWithFileRedirect(
  command: string,
  args: string[],
  cwd: string,
  timeoutMs: number,
): RunResult {
  const base = resolve(tmpdir(), `thesis-tool-${process.pid}-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  const outPath = `${base}-out.log`;
  const errPath = `${base}-err.log`;
  const fdOut = openSync(outPath, "w");
  const fdErr = openSync(errPath, "w");
  const readText = (path: string): string => {
    try {
      return readFileSync(path, "utf8");
    } catch {
      return "";
    }
  };
  try {
    const result = spawnSync(command, args, {
      cwd,
      env: process.env,
      stdio: ["ignore", fdOut, fdErr],
      windowsHide: true,
      timeout: timeoutMs,
    });
    if (result.error && isEperm(result.error)) throw new SpawnBlockedError(String(result.error));
    if (result.error) throw result.error;
    const code = typeof result.status === "number" ? result.status : result.signal ? 124 : 1;
    return { stdout: readText(outPath), stderr: readText(errPath), code, via: "file" };
  } finally {
    try {
      closeSync(fdOut);
      closeSync(fdErr);
    } catch {
      /* ignore */
    }
    for (const path of [outPath, errPath]) {
      try {
        unlinkSync(path);
      } catch {
        /* ignore */
      }
    }
  }
}

/** 运行 Python 助手：正常环境走管道（可实时进度），管道被沙箱拒绝时退回文件重定向。 */
async function runProcess(options: {
  command: string;
  args: string[];
  cwd: string;
  signal?: AbortSignal;
  onStderrLine?: (line: string) => void;
}): Promise<RunResult> {
  try {
    return await runWithPipes(
      options.command,
      options.args,
      options.cwd,
      RUN_TIMEOUT_MS,
      options.signal,
      options.onStderrLine,
    );
  } catch (error) {
    if (!(error instanceof SpawnBlockedError)) throw error;
    options.onStderrLine?.(
      "[沙箱] 管道 stdio 被拒绝（EPERM），改用文件重定向运行 Python；此模式无实时进度。",
    );
    return runWithFileRedirect(options.command, options.args, options.cwd, RUN_TIMEOUT_MS);
  }
}

function parseLastJsonLine(stdout: string): Record<string, unknown> | undefined {
  const lines = stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  for (let index = lines.length - 1; index >= 0; index -= 1) {
    try {
      const parsed: unknown = JSON.parse(lines[index]);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      /* 不是 JSON 行，继续往前找 */
    }
  }
  return undefined;
}

// ---------------------------------------------------------------------------
// 工具实现
// ---------------------------------------------------------------------------
async function ingestPdf(
  ctx: ExtensionContext,
  params: { path: string; force?: boolean },
  signal: AbortSignal | undefined,
  onUpdate: ((update: { content: [{ type: "text"; text: string }]; details: Record<string, unknown> }) => void) | undefined,
): Promise<{ content: [{ type: "text"; text: string }]; details: Record<string, unknown> }> {
  const root = projectRoot(ctx);
  const input = stripAtPrefix(params.path);
  if (!input) throw new Error("path 不能为空：请传入 PDF 文件路径");
  // 输入只读，允许项目目录之外（GUI 里用户可能选择桌面上的 PDF）。
  const pdfPath = isAbsolute(input) ? resolve(input) : resolve(root, input);
  if (!(await exists(pdfPath))) throw new Error(`找不到 PDF 文件: ${pdfPath}`);

  const helper = resolve(root, "tools", "pdf_ingest.py");
  if (!(await exists(helper))) {
    throw new Error(`找不到 PDF 摄取脚本: ${helper}（请确认仓库完整，或设置 THESIS_PYTHON 环境变量）`);
  }

  const python = process.env.THESIS_PYTHON?.trim() || "python";
  const args = [helper, "--pdf", pdfPath, "--out", ".thesis"];
  if (params.force) args.push("--force");

  const shownPdf = displayPath(root, pdfPath);
  onUpdate?.({
    content: [{ type: "text", text: `开始摄取 PDF: ${shownPdf}（上传 Moonshot Files API 并导出正文）` }],
    details: { phase: "start", pdf: shownPdf },
  });

  let progressCount = 0;
  let result: RunResult;
  try {
    result = await runProcess({
      command: python,
      args,
      cwd: root,
      signal,
      onStderrLine: (line) => {
        // 只转发助手自身的进度行，避免 Python traceback 噪声刷屏。
        if (!line.startsWith("[pdf_ingest]")) return;
        if (progressCount >= 60) return;
        progressCount += 1;
        onUpdate?.({
          content: [{ type: "text", text: line }],
          details: { phase: "progress", message: line, progress: progressCount },
        });
      },
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`PDF 摄取失败：无法启动/运行 Python（${python}）。${reason}`);
  }

  const summary = parseLastJsonLine(result.stdout);
  if (!summary) {
    const stderrTail = short(result.stderr.trim() || "（无 stderr）", 1200);
    throw new Error(
      `PDF 摄取失败（退出码 ${result.code}）：无法解析 tools/pdf_ingest.py 的 JSON 输出。\nstderr: ${stderrTail}`,
    );
  }
  if (summary.ok !== true || result.code !== 0) {
    const reason =
      typeof summary.error === "string" && summary.error.length > 0
        ? summary.error
        : `退出码 ${result.code}`;
    throw new Error(`PDF 摄取失败：${reason}`);
  }

  // 契约：工具返回 JSON 摘要文本（ok/text_path/chars/lines/headings/preview/error…）。
  return {
    content: [{ type: "text", text: JSON.stringify(summary) }],
    details: summary as Record<string, unknown>,
  };
}

async function validateOutlineTool(
  _ctx: ExtensionContext,
  params: { content: string },
): Promise<{ content: [{ type: "text"; text: string }]; details: Record<string, unknown> }> {
  const report = validateOutline(params.content ?? "");
  return {
    content: [{ type: "text", text: formatReport(report) }],
    details: {
      valid: report.valid,
      issues: report.errors,
      warnings: report.warnings,
      missing: report.missing,
      fields: report.fields,
      lineCount: report.lineCount,
    },
  };
}

async function saveOutline(
  ctx: ExtensionContext,
  params: { content: string },
): Promise<{ content: [{ type: "text"; text: string }]; details: Record<string, unknown> }> {
  const root = projectRoot(ctx);
  const report = validateOutline(params.content ?? "");
  const file = resolve(root, "outline.txt");
  const backupPath = await backupIfExists(file);
  const body = normalizeFileText(params.content ?? "");
  if (!body) throw new Error("大纲内容为空，未写入 outline.txt");
  await withFileMutationQueue(file, () => writeFile(file, body, "utf8"));

  const lines = [`已保存大纲: outline.txt（${body.trim().length} 字符 / ${report.lineCount} 行）`];
  if (backupPath) lines.push("已备份旧文件: outline.txt.bak");
  if (report.valid) {
    lines.push("模板校验: 通过（9 个字段齐全、顺序正确）");
  } else {
    lines.push(`模板校验: 未通过（${report.errors.length} 个问题），文件仍已保存以便人工修正：`);
    for (const error of report.errors.slice(0, 10)) lines.push(`  ✗ ${error}`);
  }
  for (const warning of report.warnings.slice(0, 10)) lines.push(`  ! ${warning}`);

  return {
    content: [{ type: "text", text: lines.join("\n") }],
    details: {
      saved: true,
      path: "outline.txt",
      backup: backupPath ? "outline.txt.bak" : undefined,
      valid: report.valid,
      issues: report.errors,
      warnings: report.warnings,
      chars: body.trim().length,
      lineCount: report.lineCount,
      fields: report.fields,
    },
  };
}

async function savePolished(
  ctx: ExtensionContext,
  params: { text: string; position?: string },
): Promise<{ content: [{ type: "text"; text: string }]; details: Record<string, unknown> }> {
  const root = projectRoot(ctx);
  const body = (params.text ?? "").replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n").trim();
  if (!body) throw new Error("润色文本为空，未写入 polished.txt");

  const file = resolve(root, "polished.txt");
  const backupPath = await backupIfExists(file);
  await withFileMutationQueue(file, () => writeFile(file, `${body}\n`, "utf8"));

  // 追加一条 JSONL 历史记录（含位置、字数、时间与全文），方便 GUI 追溯。
  const historyFile = resolve(root, ".thesis", "polish_history.jsonl");
  await mkdir(dirname(historyFile), { recursive: true });
  const position = typeof params.position === "string" ? params.position.trim() : "";
  const record = {
    ts: new Date().toISOString(),
    position: position.length > 0 ? position : null,
    chars: body.length,
    text: body,
  };
  await withFileMutationQueue(historyFile, () =>
    appendFile(historyFile, `${JSON.stringify(record)}\n`, "utf8"),
  );

  const positionLabel = position.length > 0 ? `，位置：${position}` : "";
  return {
    content: [
      {
        type: "text",
        text: `已保存润色文本: polished.txt（${body.length} 字符${positionLabel}）` +
          (backupPath ? "；旧文件已备份 polished.txt.bak" : "") +
          "；历史已追加 .thesis/polish_history.jsonl",
      },
    ],
    details: {
      saved: true,
      path: "polished.txt",
      chars: body.length,
      position: position.length > 0 ? position : undefined,
      backup: backupPath ? "polished.txt.bak" : undefined,
      history: ".thesis/polish_history.jsonl",
    },
  };
}

// ---------------------------------------------------------------------------
// 注册入口
// ---------------------------------------------------------------------------
export default function thesisToolsExtension(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "thesis_pdf_ingest",
    label: "Thesis PDF Ingest",
    description:
      "把论文 PDF 上传到 Moonshot Files API（purpose=file-extract），导出完整正文到 .thesis/pdf_text.txt，" +
      "并返回 JSON 摘要（ok/text_path/chars/lines/headings/preview/tail_preview/error）。" +
      "preview 含题目/摘要开头、headings 含章节标题、tail_preview 含结论/参考文献附近内容；" +
      "若这三者已足够填写大纲，可不再调用 read（低 RPM 账号尤其重要）。" +
      "需要完整正文时再用 read 读取 .thesis/pdf_text.txt（不要直接读 PDF）。",
    promptSnippet:
      "thesis_pdf_ingest: 上传论文 PDF 并导出正文到 .thesis/pdf_text.txt（返回 JSON 摘要）",
    promptGuidelines: [
      "需要根据 PDF 生成大纲时，先调用 thesis_pdf_ingest；优先用返回的 preview/headings/tail_preview 生成大纲，信息不足时再用 read 读取 .thesis/pdf_text.txt；不要凭 PDF 文件名猜测内容。",
    ],
    parameters: Type.Object({
      path: Type.String({ description: "PDF 路径：绝对路径，或相对项目根目录的路径" }),
      force: Type.Optional(
        Type.Boolean({ description: "忽略 sha1 缓存，强制重新上传 Moonshot Files API（默认 false）" }),
      ),
    }),
    async execute(_toolCallId, params, signal, onUpdate, ctx) {
      return ingestPdf(ctx, params, signal, onUpdate as never);
    },
  });

  pi.registerTool({
    name: "thesis_outline_validate",
    label: "Thesis Outline Validate",
    description:
      "确定性校验九行论文大纲模板（题目/摘要/第一章题目/第二章题目/第三章题目/第四章题目/" +
      "第五章题目/总结/参考文献，缺失内容写“未提供”）。返回可直接阅读的中文报告，" +
      "details 里是 {valid, issues[], warnings[], missing[], fields, lineCount}。不读写文件。",
    promptSnippet: "thesis_outline_validate: 校验九行大纲模板结构是否合法",
    parameters: Type.Object({
      content: Type.String({
        description:
          "待校验的完整大纲文本：恰好 9 行，每行“字段名：内容”，顺序固定，缺失写“未提供”",
      }),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      return validateOutlineTool(ctx, params);
    },
  });

  pi.registerTool({
    name: "thesis_outline_save",
    label: "Thesis Outline Save",
    description:
      "校验并把九行大纲写入项目根目录 outline.txt（UTF-8；已有文件先备份为 outline.txt.bak）。" +
      "返回保存路径、字数与校验警告；模板不合法时仍会保存以便人工修正，但会在返回值中列出问题。",
    promptSnippet: "thesis_outline_save: 校验并保存 outline.txt（自动备份 outline.txt.bak）",
    promptGuidelines: [
      "生成或修改大纲后必须调用 thesis_outline_save 落盘；不要用 write/edit 直接写 outline.txt。",
    ],
    parameters: Type.Object({
      content: Type.String({ description: "要保存的九行大纲全文" }),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      return saveOutline(ctx, params);
    },
  });

  pi.registerTool({
    name: "thesis_save_polished",
    label: "Thesis Save Polished",
    description:
      "把润色后的论文文本写入项目根目录 polished.txt（UTF-8，旧文件备份为 polished.txt.bak），" +
      "并向 .thesis/polish_history.jsonl 追加一条 JSON 记录（时间/位置/字数/全文）。",
    promptSnippet: "thesis_save_polished: 保存润色结果到 polished.txt 并记录历史",
    promptGuidelines: [
      "润色完成后必须调用 thesis_save_polished 保存结果；不要用 write/edit 直接写 polished.txt。",
    ],
    parameters: Type.Object({
      text: Type.String({ description: "润色后的正文（纯文本，不要包含解释或 Markdown 代码块）" }),
      position: Type.Optional(
        Type.String({ description: "润色位置，如“第三章 3.2 节”或“摘要”（可选，写入历史记录）" }),
      ),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      return savePolished(ctx, params);
    },
  });
}
