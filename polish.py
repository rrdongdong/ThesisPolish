# -*- coding: utf-8 -*-
"""旧版流程：按大纲润色局部段落（单次 chat 调用，低 RPM 账号兜底）。

不依赖 openai SDK（改用 requests 直连，规避 openai 3.x/httpx2 解压兼容问题）。

用法：
    set MOONSHOT_API_KEY=sk-...
    python polish.py --position "第三章 3.2" --text "待润色文本" --model kimi-k2.6
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

SYSTEM_PROMPT = (
    "你是学术写作润色助手。只基于给定信息润色，不编造事实，不新增实验结果，不改变原意。"
    "保留术语与引用标记风格。"
)


def build_messages(outline_content: str, position: str, text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "请根据以下论文大纲信息和指定位置，对输入文本进行学术中文润色。\n"
                "要求：\n"
                "1) 语义不变，逻辑更清晰，表达更规范；\n"
                "2) 不补充大纲中没有的事实；\n"
                "3) 输出仅为润色后的最终文本，不要解释。\n\n"
                f"【大纲信息】\n{outline_content}\n\n"
                f"【润色位置】\n{position}\n\n"
                f"【待润色文本】\n{text}\n"
            ),
        },
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按大纲润色局部段落（单次 chat 调用）")
    parser.add_argument("--outline", default="outline.txt")
    parser.add_argument("--position", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", default="polished.txt")
    parser.add_argument(
        "--model",
        default=moonshot_chat.default_model(),
        help="Moonshot 模型 id（默认取 THESIS_MODEL 或 kimi-k2-0905-preview）",
    )
    parser.add_argument(
        "--retry-base",
        type=float,
        default=2.0,
        help="429/5xx 重试的基础等待秒数（指数退避）",
    )
    args = parser.parse_args(argv)

    outline_path = Path(args.outline)
    if not outline_path.exists():
        print(f"处理失败: 未找到文件 {outline_path}", file=sys.stderr)
        return 1
    outline_content = outline_path.read_text(encoding="utf-8")

    try:
        polished = moonshot_chat.chat_completion(
            args.model,
            build_messages(outline_content, args.position, args.text),
            retry_base=args.retry_base,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"处理失败: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output)
    output_path.write_text(polished, encoding="utf-8")
    print(f"{output_path} 已生成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
