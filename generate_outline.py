# -*- coding: utf-8 -*-
"""旧版流程：论文 PDF -> 九行大纲（单次 chat 调用，低 RPM 账号兜底）。

与 pi agent 流程的区别：
  * 只有 1 次 chat 调用（PDF 上传/抽取走 tools/pdf_ingest.py 的 Files API，
    不算 chat 请求），因此在 Moonshot 组织 ``max RPM: 3`` 之类限制下也能稳定完成；
  * 不依赖 openai SDK（改用 requests 直连，规避 openai 3.x/httpx2 解压兼容问题）。

用法：
    set MOONSHOT_API_KEY=sk-...
    python generate_outline.py REALM.pdf --model kimi-k2.6
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
TOOLS_DIR = BASE_DIR / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import moonshot_chat  # noqa: E402
import pdf_ingest  # noqa: E402

SYSTEM_PROMPT = (
    "你是 Kimi，由 Moonshot AI 提供的人工智能助手，你更擅长中文和英文的对话。"
    "你会为用户提供安全，有帮助，准确的回答。同时，你会拒绝一切涉及恐怖主义，"
    "种族歧视，黄色暴力等问题的回答。Moonshot AI 为专有名词，不可翻译成其他语言。"
)

OUTLINE_PROMPT = (
    "仅基于给定论文文本抽取信息，不得补写或臆造；缺失内容统一写“未提供”。"
    "请严格按以下纯文本模板输出（字段名与顺序不可变）：\n"
    "题目：<论文题目或未提供>\n"
    "摘要：<100字内摘要或未提供>\n"
    "第一章题目：<章节名或未提供>\n"
    "第二章题目：<章节名或未提供>\n"
    "第三章题目：<章节名或未提供>\n"
    "第四章题目：<章节名或未提供>\n"
    "第五章题目：<章节名或未提供>\n"
    "总结：<80字内总结或未提供>\n"
    "参考文献：<有/无/未提供>\n"
    "除以上9行外不要输出任何解释、标点装饰或额外内容。"
)


def build_messages(text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": text},
        {"role": "user", "content": OUTLINE_PROMPT},
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PDF -> 九行大纲（单次 chat 调用）")
    parser.add_argument("pdf", nargs="?", default="test2.pdf")
    parser.add_argument(
        "--model",
        default=moonshot_chat.default_model(),
        help="Moonshot 模型 id（默认取 THESIS_MODEL 或 kimi-k2-0905-preview）",
    )
    parser.add_argument("--output", default="outline.txt")
    parser.add_argument("--out-dir", default=".thesis", help="PDF 正文缓存目录")
    parser.add_argument(
        "--retry-base",
        type=float,
        default=2.0,
        help="429/5xx 重试的基础等待秒数（指数退避）",
    )
    args = parser.parse_args(argv)

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"处理失败: 未找到文件 {pdf_path}", file=sys.stderr)
        return 1

    # 1) PDF -> 正文（Moonshot Files API，带 sha1 缓存；不是 chat 请求，不占 RPM）
    try:
        summary = pdf_ingest.ingest(str(pdf_path), out=args.out_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"处理失败: {exc}", file=sys.stderr)
        return 1
    if not summary.get("ok"):
        print(f"处理失败: {summary.get('error') or 'PDF 正文提取失败'}", file=sys.stderr)
        return 1
    text_path = Path(str(summary.get("text_path") or ""))
    try:
        file_content = text_path.read_text(encoding="utf-8")[:120000]
    except OSError as exc:
        print(f"处理失败: 读取正文失败：{exc}", file=sys.stderr)
        return 1

    # 2) 单次 chat 调用生成大纲
    try:
        outline_text = moonshot_chat.chat_completion(
            args.model,
            build_messages(file_content),
            retry_base=args.retry_base,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"处理失败: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    output_path.write_text(outline_text, encoding="utf-8")
    print(f"{output_path} 已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
