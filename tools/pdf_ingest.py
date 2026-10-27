#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PDF / 文档摄取助手（ThesisPolish）。

把 `generate_outline.py` 里内联的 Moonshot Files API 逻辑抽成独立、可测试的 CLI：
上传 PDF（`purpose=file-extract`）→ 下载抽取出的纯文本 → 本地 sha1 缓存 → 给 pi
agent 的 `thesis_pdf_ingest` 工具 / Python 运行时调用。

用法::

    python tools/pdf_ingest.py --pdf 论文.pdf [--out .thesis] [--force]

环境变量::

    MOONSHOT_API_KEY      必需，Moonshot/Kimi API 密钥
    MOONSHOT_BASE_URL     可选，默认 https://api.moonshot.cn/v1
    THESIS_HTTP_TIMEOUT   可选，HTTP 超时秒数，默认 180

产物::

    {out}/pdf_text.txt                当前 PDF 的完整正文（UTF-8，agent 直接 read）
    {out}/pdf/<sha1>.txt              sha1 内容缓存
    {out}/pdf/<sha1>.meta.json        缓存元数据

输出约定（供 pi 扩展解析）::

    stdout 只打印 **一行** JSON：
      {"ok":true,"text_path":"...","chars":123,"lines":45,
       "headings":[{"level":1,"title":"摘要","line":3}],"preview":"...","error":null,...}
    进度/诊断信息全部写 stderr（不影响 stdout 解析）。
    成功退出码 0，失败退出码 1（此时 stdout 同样是一行 JSON，ok=false、error=中文原因）。

只依赖标准库 + requests（见 requirements.txt）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:  # requests 是唯一第三方依赖；缺失时也要给出可读的 JSON 错误。
    import requests
except Exception:  # pragma: no cover - 仅在依赖缺失的环境触发
    requests = None  # type: ignore[assignment]

HELPER_VERSION = "1.0"
DEFAULT_BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_OUT_DIR = ".thesis"
DEFAULT_TIMEOUT = 180.0
PREVIEW_CHARS = 3000
TAIL_PREVIEW_CHARS = 1500
MAX_HEADINGS = 500
MAX_ERROR_BODY = 300


# ---------------------------------------------------------------------------
# 错误类型 / 工具函数
# ---------------------------------------------------------------------------
class PdfIngestError(RuntimeError):
    """可预期的、需要以中文展示给用户的失败。"""


def _progress(message: str) -> None:
    """进度写 stderr；调用方（pi 扩展）会把它转成流式进度。"""
    try:
        print(f"[pdf_ingest] {message}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - stderr 不可写时不影响主流程
        pass


def _configure_stdio() -> None:
    """Windows 中文系统默认 GBK；强制 UTF-8 以免 JSON/中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            pass


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _error_body(response: Any) -> str:
    """截取 HTTP 错误响应体，便于在 GUI 里定位问题。"""
    try:
        body = response.content.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover
        body = str(getattr(response, "text", ""))
    body = " ".join(body.split())
    return body[:MAX_ERROR_BODY]


# ---------------------------------------------------------------------------
# 标题检测：摘要 / Abstract / 第X章 / 1.2.3 / 参考文献
# ---------------------------------------------------------------------------
_MARKDOWN_PREFIX = re.compile(r"^#{1,6}\s*")
_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)[.、．:：]?\s*(\S.*)$")
_CHAPTER_HEADING = re.compile(r"^(第[一二三四五六七八九十百零两0-9]+章)\s*[:：.、]?\s*(.*)$")
_ABSTRACT_CN = re.compile(r"^(摘\s*要)\s*[:：]?\s*(.*)$")
_ABSTRACT_EN = re.compile(r"^(abstract)\b\s*[:：.]?\s*(.*)$", re.IGNORECASE)
_REFERENCES = re.compile(r"^(参考文献|references|bibliography)\b\s*[:：]?\s*(.*)$", re.IGNORECASE)
_EN_SECTION = re.compile(
    r"^(introduction|related works?|background|preliminar(?:y|ies)|methodology|methods?|"
    r"approach|experiments?|evaluation|results?|discussion|conclusions?|"
    r"acknowledge?ments?)\b\s*[:：.]?\s*(.*)$",
    re.IGNORECASE,
)


def _trim_title(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def detect_headings(text: str) -> list[dict[str, Any]]:
    """按行扫描正文，返回 [{level,title,line}]（line 从 1 开始）。

    识别规则：
      * 摘要 / Abstract / 参考文献 / References            -> level 1
      * 第X章（第一章、第 3 章…）                          -> level 1
      * Introduction / Related Work / Method 等英文章节名  -> level 1
      * 1.2.3 多级编号小标题                                -> level = 编号段数
      * Markdown 标题行（如 ``# 1.Introduction``）先去掉 ``#`` 前缀再判断
    """
    headings: list[dict[str, Any]] = []
    if not text:
        return headings
    for index, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or len(stripped) > 120:
            continue
        has_markdown = bool(_MARKDOWN_PREFIX.match(stripped))
        line = _MARKDOWN_PREFIX.sub("", stripped).strip()
        if not line:
            continue

        match = _ABSTRACT_CN.match(line)
        if match:
            headings.append({"level": 1, "title": "摘要", "line": index})
            continue
        match = _ABSTRACT_EN.match(line)
        if match:
            headings.append({"level": 1, "title": "Abstract", "line": index})
            continue
        match = _REFERENCES.match(line)
        if match:
            headings.append({"level": 1, "title": match.group(1), "line": index})
            continue
        match = _CHAPTER_HEADING.match(line)
        if match:
            title = f"{match.group(1)} {_trim_title(match.group(2))}".strip()
            headings.append({"level": 1, "title": _trim_title(title), "line": index})
            continue
        match = _EN_SECTION.match(line)
        if match and (has_markdown or len(line) <= 40):
            headings.append({"level": 1, "title": match.group(1), "line": index})
            continue
        match = _NUMBERED_HEADING.match(line)
        if match and (has_markdown or "." in match.group(1)):
            number, title = match.group(1), _trim_title(match.group(2))
            if not title:
                continue
            level = number.count(".") + 1
            headings.append({"level": level, "title": f"{number} {title}", "line": index})

        if len(headings) >= MAX_HEADINGS:
            break
    return headings


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


# ---------------------------------------------------------------------------
# Moonshot Files API
# ---------------------------------------------------------------------------
def _api_base() -> str:
    return (os.getenv("MOONSHOT_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")


def _api_key() -> str:
    key = (os.getenv("MOONSHOT_API_KEY") or "").strip()
    if not key:
        raise PdfIngestError(
            "缺少环境变量 MOONSHOT_API_KEY：请先在 GUI 中填写 Kimi API 密钥，"
            "或先在终端执行 set MOONSHOT_API_KEY=sk-..."
        )
    return key


def _timeout() -> float:
    raw = (os.getenv("THESIS_HTTP_TIMEOUT") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def _request_error(prefix: str, error: Exception) -> PdfIngestError:
    detail = str(error) or error.__class__.__name__
    return PdfIngestError(f"{prefix}：{detail}（请检查网络、代理与 MOONSHOT_BASE_URL）")


def upload_pdf(pdf_path: Path, api_key: str, api_base: str, timeout: float) -> str:
    """上传 PDF 到 Files API，返回 file_id。"""
    if requests is None:  # pragma: no cover
        raise PdfIngestError("缺少 Python 依赖 requests，请执行 pip install -r requirements.txt")
    _progress(f"上传 PDF 到 {api_base}/files（purpose=file-extract）: {pdf_path.name}")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with pdf_path.open("rb") as handle:
            response = requests.post(
                f"{api_base}/files",
                headers=headers,
                files={"file": (pdf_path.name, handle, "application/pdf")},
                data={"purpose": "file-extract"},
                timeout=timeout,
            )
    except Exception as error:  # requests 网络异常（连接/超时/代理）
        raise _request_error("上传 PDF 失败（网络错误）", error) from error

    if response.status_code != 200:
        message = _error_body(response)
        if response.status_code in (401, 403):
            raise PdfIngestError(
                f"Files API 鉴权失败（HTTP {response.status_code}）：请检查 MOONSHOT_API_KEY 是否有效。{message}"
            )
        if response.status_code == 429:
            raise PdfIngestError(f"Files API 限流（HTTP 429）：请稍后重试。{message}")
        raise PdfIngestError(f"上传 PDF 失败（HTTP {response.status_code}）：{message}")

    try:
        payload = response.json()
    except ValueError as error:
        raise PdfIngestError(f"Files API 返回内容不是 JSON：{_error_body(response)}") from error
    file_id = payload.get("id") if isinstance(payload, dict) else None
    if not file_id:
        raise PdfIngestError(f"Files API 未返回 file id：{_error_body(response)}")
    _progress(f"上传成功，file_id={file_id}")
    return str(file_id)


def download_text(file_id: str, api_key: str, api_base: str, timeout: float) -> str:
    """下载 Files API 抽取出的正文。"""
    if requests is None:  # pragma: no cover
        raise PdfIngestError("缺少 Python 依赖 requests，请执行 pip install -r requirements.txt")
    _progress(f"下载抽取正文（file_id={file_id}）")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(
            f"{api_base}/files/{file_id}/content", headers=headers, timeout=timeout
        )
    except Exception as error:  # 网络异常
        raise _request_error("下载正文失败（网络错误）", error) from error

    if response.status_code != 200:
        message = _error_body(response)
        if response.status_code in (401, 403):
            raise PdfIngestError(
                f"Files API 鉴权失败（HTTP {response.status_code}）：请检查 MOONSHOT_API_KEY。{message}"
            )
        raise PdfIngestError(f"下载正文失败（HTTP {response.status_code}）：{message}")

    # 官方接口直接返回 text/plain；兼容部分代理/兼容实现返回 JSON 包装的情况。
    text = response.content.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            for key in ("content", "text", "data"):
                value = payload.get(key)
                if isinstance(value, str):
                    text = value
                    break
    if not text.strip():
        raise PdfIngestError("Files API 返回的正文为空：PDF 可能是扫描件（无文字层）或文档已损坏。")
    _progress(f"正文下载完成：{len(text)} 字符")
    return text


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def _load_cached(cache_txt: Path, cache_meta: Path) -> tuple[str, dict[str, Any]] | None:
    if not cache_txt.is_file() or cache_txt.stat().st_size == 0:
        return None
    try:
        text = _read_text(cache_txt)
    except OSError:
        return None
    if not text.strip():
        return None
    meta: dict[str, Any] = {}
    if cache_meta.is_file():
        try:
            loaded = json.loads(cache_meta.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, ValueError):
            meta = {}
    return text, meta


def _save_cache(cache_txt: Path, cache_meta: Path, text: str, pdf_path: Path, sha1: str,
                remote_file_id: str, api_base: str) -> None:
    _atomic_write_text(cache_txt, text)
    meta = {
        "helper_version": HELPER_VERSION,
        "sha1": sha1,
        "source_pdf": str(pdf_path),
        "source_bytes": pdf_path.stat().st_size,
        "remote_file_id": remote_file_id,
        "api_base": api_base,
        "chars": len(text),
        "lines": _count_lines(text),
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    _atomic_write_text(cache_meta, json.dumps(meta, ensure_ascii=False, indent=2) + "\n")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def ingest(pdf: str, out: str = DEFAULT_OUT_DIR, force: bool = False) -> dict[str, Any]:
    """执行摄取，成功返回 summary dict（不打印）。失败抛 PdfIngestError。"""
    pdf_path = Path(pdf).expanduser()
    if not pdf_path.is_absolute():
        pdf_path = (Path.cwd() / pdf_path).resolve()
    if not pdf_path.exists():
        raise PdfIngestError(f"找不到 PDF 文件：{pdf_path}")
    if not pdf_path.is_file():
        raise PdfIngestError(f"PDF 路径不是普通文件：{pdf_path}")
    if pdf_path.stat().st_size <= 0:
        raise PdfIngestError(f"PDF 文件为空：{pdf_path}")

    out_dir = Path(out).expanduser()
    if not out_dir.is_absolute():
        out_dir = (Path.cwd() / out_dir).resolve()
    cache_dir = out_dir / "pdf"
    current_text_path = out_dir / "pdf_text.txt"

    sha1 = _sha1_file(pdf_path)
    cache_txt = cache_dir / f"{sha1}.txt"
    cache_meta = cache_dir / f"{sha1}.meta.json"
    api_base = _api_base()

    cached = False
    remote_file_id = ""
    cache_hit = None if force else _load_cached(cache_txt, cache_meta)
    if cache_hit is not None:
        text, meta = cache_hit
        cached = True
        remote_file_id = str(meta.get("remote_file_id") or "")
        _progress(f"缓存命中（sha1={sha1[:12]}…），跳过上传：{cache_txt}")
    else:
        api_key = _api_key()
        remote_file_id = upload_pdf(pdf_path, api_key, api_base, _timeout())
        text = download_text(remote_file_id, api_key, api_base, _timeout())
        _save_cache(cache_txt, cache_meta, text, pdf_path, sha1, remote_file_id, api_base)
        _progress(f"已写入缓存：{cache_txt}")

    _atomic_write_text(current_text_path, text)
    _progress(f"当前正文已写入：{current_text_path}（{len(text)} 字符）")

    headings = detect_headings(text)
    return {
        "ok": True,
        "text_path": str(current_text_path),
        "chars": len(text),
        "lines": _count_lines(text),
        "headings": headings,
        "preview": text[:PREVIEW_CHARS],
        "tail_preview": text[-TAIL_PREVIEW_CHARS:] if text else "",
        "error": None,
        "cached": cached,
        "sha1": sha1,
        "pdf": str(pdf_path),
        "remote_file_id": remote_file_id,
    }


def _failure(error: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "ok": False,
        "text_path": None,
        "chars": 0,
        "lines": 0,
        "headings": [],
        "preview": "",
        "tail_preview": "",
        "error": error,
    }
    if extra:
        summary.update(extra)
    return summary


def _print_summary(summary: dict[str, Any]) -> None:
    # 契约：stdout 只有这一行 JSON（UTF-8），供扩展/测试机读。
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="上传 PDF 到 Moonshot Files API 并导出正文（sha1 缓存，stdout 输出一行 JSON）"
    )
    parser.add_argument("--pdf", required=True, help="PDF 文件路径")
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT_DIR,
        help=f"输出目录（默认 {DEFAULT_OUT_DIR}；正文写入 <out>/pdf_text.txt，缓存在 <out>/pdf/）",
    )
    parser.add_argument("--force", action="store_true", help="忽略已有缓存，强制重新上传/下载")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    _configure_stdio()
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        summary = ingest(args.pdf, args.out, force=args.force)
    except PdfIngestError as error:
        _progress(f"失败：{error}")
        _print_summary(_failure(str(error)))
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        _print_summary(_failure("已取消"))
        return 1
    except Exception as error:  # 兜底：任何异常都转成 JSON，避免扩展解析失败
        _progress(f"未预期错误：{error!r}")
        _print_summary(_failure(f"PDF 摄取失败（未预期错误）：{error}"))
        return 1
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
