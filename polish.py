from pathlib import Path
from openai import OpenAI, RateLimitError
import argparse
import os
import sys
import time


def create_with_retry(client: OpenAI, model: str, messages: list, max_attempts: int = 6):
    delay = 2
    for attempt in range(1, max_attempts + 1):
        try:
            return client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.3,
            )
        except RateLimitError:
            if attempt == max_attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--outline", default="outline.txt")
    parser.add_argument("--position", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", default="polished.txt")
    parser.add_argument("--model", default=os.getenv("THESIS_MODEL", "kimi-k2-0905-preview"))
    args = parser.parse_args()

    outline_path = Path(args.outline)
    if not outline_path.exists():
        print(f"处理失败: 未找到文件 {outline_path}", file=sys.stderr)
        return 1

    api_key = os.getenv("MOONSHOT_API_KEY")
    if not api_key:
        print("处理失败: 未设置环境变量 MOONSHOT_API_KEY", file=sys.stderr)
        return 1

    outline_content = outline_path.read_text(encoding="utf-8")
    client = OpenAI(
        api_key=os.getenv("MOONSHOT_API_KEY"),
        base_url="https://api.moonshot.cn/v1",
    )

    messages = [
        {
            "role": "system",
            "content": "你是学术写作润色助手。只基于给定信息润色，不编造事实，不新增实验结果，不改变原意。保留术语与引用标记风格。",
        },
        {
            "role": "user",
            "content": (
                "请根据以下论文大纲信息和指定位置，对输入文本进行学术中文润色。\n"
                "要求：\n"
                "1) 语义不变，逻辑更清晰，表达更规范；\n"
                "2) 不补充大纲中没有的事实；\n"
                "3) 输出仅为润色后的最终文本，不要解释。\n\n"
                f"【大纲信息】\n{outline_content}\n\n"
                f"【润色位置】\n{args.position}\n\n"
                f"【待润色文本】\n{args.text}\n"
            ),
        },
    ]

    try:
        completion = create_with_retry(client, args.model, messages)
        polished = completion.choices[0].message.content or ""
        if not polished.strip():
            print("处理失败: API返回空内容", file=sys.stderr)
            return 1
        Path(args.output).write_text(polished, encoding="utf-8")
        print(f"{args.output} 已生成")
        return 0
    except Exception as e:
        print(f"处理失败: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())