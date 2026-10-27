from pathlib import Path
from openai import OpenAI
from openai import RateLimitError
import argparse
import os
import sys
import time
 
parser = argparse.ArgumentParser()
parser.add_argument("pdf", nargs="?", default="test2.pdf")
# 低 RPM 账号可用传统流程（单次 chat 调用）作为兜底；模型由 GUI 的「Agent 模型」传入。
parser.add_argument(
    "--model",
    default=os.getenv("THESIS_MODEL", "kimi-k2-0905-preview"),
    help="Moonshot 模型 id（默认取 THESIS_MODEL 或 kimi-k2-0905-preview）",
)
args = parser.parse_args()
pdf_path = Path(args.pdf)
if not pdf_path.exists():
    print(f"处理失败: 未找到文件 {pdf_path}", file=sys.stderr)
    raise SystemExit(1)

client = OpenAI(
    api_key=os.getenv("MOONSHOT_API_KEY"),
    base_url = "https://api.moonshot.cn/v1",
)

file_object = client.files.create(file=pdf_path, purpose="file-extract")
 
# 获取结果
# file_content = client.files.retrieve_content(file_id=file_object.id)
# 注意，之前 retrieve_content api 在最新版本标记了 warning, 可以用下面这行代替
# 如果是旧版本，可以用 retrieve_content
file_content = client.files.content(file_id=file_object.id).text
file_content = file_content[:120000]
 
# 把它放进请求中
messages = [
    {
        "role": "system",
        "content": "你是 Kimi，由 Moonshot AI 提供的人工智能助手，你更擅长中文和英文的对话。你会为用户提供安全，有帮助，准确的回答。同时，你会拒绝一切涉及恐怖主义，种族歧视，黄色暴力等问题的回答。Moonshot AI 为专有名词，不可翻译成其他语言。",
    },
    {
        "role": "system",
        "content": file_content,
    },
    {"role": "user", "content": "仅基于给定论文文本抽取信息，不得补写或臆造；缺失内容统一写“未提供”。请严格按以下纯文本模板输出（字段名与顺序不可变）：\n题目：<论文题目或未提供>\n摘要：<100字内摘要或未提供>\n第一章题目：<章节名或未提供>\n第二章题目：<章节名或未提供>\n第三章题目：<章节名或未提供>\n第四章题目：<章节名或未提供>\n第五章题目：<章节名或未提供>\n总结：<80字内总结或未提供>\n参考文献：<有/无/未提供>\n除以上9行外不要输出任何解释、标点装饰或额外内容。"},
]
 
# 然后调用 chat-completion, 获取 Kimi 的回答
def create_with_retry(max_attempts: int = 6):
    delay = 2
    for attempt in range(1, max_attempts + 1):
        try:
            return client.chat.completions.create(
                model=args.model,
                messages=messages,
                temperature=0.3,
            )
        except RateLimitError:
            if attempt == max_attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30)

try:
    completion = create_with_retry()
    outline_text = completion.choices[0].message.content or ""
    Path("outline.txt").write_text(outline_text, encoding="utf-8")
    print("outline.txt 已生成")
except Exception as e:
    print(f"处理失败: {e}", file=sys.stderr)
    raise SystemExit(1)