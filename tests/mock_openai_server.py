"""离线 OpenAI 兼容 mock 服务（供 ThesisPolish 集成测试使用）。

同时模拟两类接口，便于在没有真实网络/API Key 的环境里跑通完整 agent 链路：

1) OpenAI Chat Completions（pi 的 `openai-completions` provider 调用）
   POST /v1/chat/completions   —— 默认流式 SSE，也支持非流式
2) Moonshot Files API（`tools/pdf_ingest.py` 调用）
   POST /v1/files              —— multipart 上传，返回 {"id": "file-xxx", ...}
   GET  /v1/files/{id}/content —— 返回纯文本正文

场景由环境变量控制（每次测试启动一个独立服务实例即可）：
   MOCK_FINAL_TEXT   最终文本（默认 "mock 回复完成"）
   MOCK_TOOL_STEPS   JSON 数组：[{"name": "...", "arguments": {...}}, ...]
                     第 i 次模型调用返回第 i 个工具调用；用尽后返回最终文本。
   MOCK_FILES_TEXT   Files API 返回的论文正文（默认一段中文示例正文）
   MOCK_FAIL_STREAK  前 N 次 chat 调用返回 HTTP 500（测重试/错误分支，默认 0）
   MOCK_DELAY        每个 SSE chunk 前的延迟秒数（测取消/流式，默认 0）
   MOCK_MODELS       GET /v1/models 返回的模型 ID，逗号分隔
                     （默认 "kimi-k2-0905-preview"）
   MOCK_BALANCE      GET /v1/users/me/balance 返回的 JSON（默认含 available_balance 42.5）
   MOCK_LOG          请求日志 JSONL 路径（默认 mock_requests.jsonl）

辅助端点（测试断言用）：
   GET /_mock/state   {"chat_calls": n, "uploads": n, "last_request": {...}}
   POST /_mock/reset  清空计数

命令行：python tests/mock_openai_server.py [port]
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_LOCK = threading.Lock()
STATE = {
    "chat_calls": 0,
    "uploads": 0,
    "last_request": None,
    "fail_streak_left": int(os.environ.get("MOCK_FAIL_STREAK", "0") or 0),
}
FILES: dict[str, str] = {}


def _now() -> int:
    return int(time.time())


def _env_json(name: str, default):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


FINAL_TEXT = os.environ.get("MOCK_FINAL_TEXT", "mock 回复完成")
TOOL_STEPS = _env_json("MOCK_TOOL_STEPS", [])
FILES_TEXT = os.environ.get(
    "MOCK_FILES_TEXT",
    "题目：基于深度学习的示例论文\n摘要：这是一段用于离线测试的论文正文。\n"
    "第一章题目：绪论\n第二章题目：相关工作\n第三章题目：方法\n"
    "第四章题目：实验\n第五章题目：总结与展望\n总结：示例总结。\n参考文献：有\n",
)
MOCK_DELAY = float(os.environ.get("MOCK_DELAY", "0") or 0)
MOCK_MODELS = [
    item.strip()
    for item in os.environ.get("MOCK_MODELS", "kimi-k2-0905-preview").split(",")
    if item.strip()
]
MOCK_BALANCE = _env_json(
    "MOCK_BALANCE",
    {
        "code": 0,
        "data": {
            "available_balance": 42.5,
            "voucher_balance": 10.0,
            "cash_balance": 32.5,
        },
        "status": True,
    },
)
MOCK_LOG = os.environ.get("MOCK_LOG", "mock_requests.jsonl")


def _log(path: str, payload: dict) -> None:
    try:
        with open(MOCK_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": path, **payload}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _sse(obj: dict) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8")


def _chunk(model: str, delta: dict, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": _now(),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _extract_messages(body: dict) -> list[dict]:
    return body.get("messages") or []


def _has_tool_result(messages: list[dict]) -> bool:
    return any(m.get("role") in ("tool", "toolResult") for m in messages)


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return " ".join(
                    str(x.get("text", "")) for x in content if isinstance(x, dict)
                )
    return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- helpers
    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, obj: dict, status: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_text(self, text: str, status: int = 200) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ---------------------------------------------------------------- routing
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/v1/models"):
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {"id": model_id, "object": "model"} for model_id in MOCK_MODELS
                    ],
                }
            )
            return
        if self.path == "/_mock/state":
            with STATE_LOCK:
                self._send_json(dict(STATE))
            return
        if self.path.startswith("/v1/users/me/balance"):
            self._send_json(MOCK_BALANCE)
            return
        if self.path.startswith("/v1/files/") and self.path.endswith("/content"):
            file_id = self.path[len("/v1/files/") : -len("/content")]
            text = FILES.get(file_id, FILES_TEXT)
            self._send_text(text)
            return
        self._send_json({"error": {"message": f"unknown path {self.path}"}}, 404)

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/v1/chat/completions"):
            self._handle_chat()
            return
        if self.path.startswith("/v1/files"):
            self._handle_upload()
            return
        if self.path == "/_mock/reset":
            with STATE_LOCK:
                STATE["chat_calls"] = 0
                STATE["uploads"] = 0
                STATE["last_request"] = None
                STATE["fail_streak_left"] = int(
                    os.environ.get("MOCK_FAIL_STREAK", "0") or 0
                )
            self._send_json({"ok": True})
            return
        self._send_json({"error": {"message": f"unknown path {self.path}"}}, 404)

    # ---------------------------------------------------------------- handlers
    def _handle_upload(self) -> None:
        raw = self._read_body()
        with STATE_LOCK:
            STATE["uploads"] += 1
            file_id = f"file-mock-{STATE['uploads']}"
            FILES[file_id] = FILES_TEXT
        _log(self.path, {"event": "upload", "bytes": len(raw), "file_id": file_id})
        self._send_json(
            {
                "id": file_id,
                "object": "file",
                "bytes": len(raw),
                "created_at": _now(),
                "filename": "mock.pdf",
                "purpose": "file-extract",
            }
        )

    def _handle_chat(self) -> None:
        try:
            body = json.loads(self._read_body().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._send_json({"error": {"message": "bad json"}}, 400)
            return
        messages = _extract_messages(body)
        model = body.get("model", "kimi-k2-0905-preview")
        stream = bool(body.get("stream"))
        with STATE_LOCK:
            STATE["chat_calls"] += 1
            call_no = STATE["chat_calls"]
            STATE["last_request"] = body
            fail = STATE["fail_streak_left"] > 0
            if fail:
                STATE["fail_streak_left"] -= 1
        _log(
            self.path,
            {
                "event": "chat",
                "call": call_no,
                "model": model,
                "stream": stream,
                "has_tool_result": _has_tool_result(messages),
                "preview": _last_user_text(messages)[:160],
            },
        )
        if fail:
            self._send_json(
                {"error": {"message": "mock upstream exploded", "type": "server_error"}},
                500,
            )
            return

        # 第 n 次模型调用返回第 n 个工具调用；全部用尽后返回最终文本。
        tool_step = None
        if 0 <= call_no - 1 < len(TOOL_STEPS):
            tool_step = TOOL_STEPS[call_no - 1]

        if not stream:
            if tool_step:
                self._send_json(
                    {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion",
                        "created": _now(),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": f"call_mock_{call_no}",
                                            "type": "function",
                                            "function": {
                                                "name": tool_step["name"],
                                                "arguments": json.dumps(
                                                    tool_step.get("arguments", {}),
                                                    ensure_ascii=False,
                                                ),
                                            },
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                )
            else:
                self._send_json(
                    {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion",
                        "created": _now(),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": FINAL_TEXT},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )
            return

        # 流式 SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def emit(obj: dict) -> None:
            if MOCK_DELAY:
                time.sleep(MOCK_DELAY)
            data = _sse(obj)
            self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n")
            self.wfile.flush()

        try:
            emit(_chunk(model, {"role": "assistant", "content": ""}))
            if tool_step:
                emit(
                    _chunk(
                        model,
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": f"call_mock_{call_no}",
                                    "type": "function",
                                    "function": {
                                        "name": tool_step["name"],
                                        "arguments": json.dumps(
                                            tool_step.get("arguments", {}),
                                            ensure_ascii=False,
                                        ),
                                    },
                                }
                            ]
                        },
                    )
                )
                emit(_chunk(model, {}, finish="tool_calls"))
            else:
                for ch in FINAL_TEXT:
                    emit(_chunk(model, {"content": ch}))
                emit(_chunk(model, {}, finish="stop"))
            emit(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion.chunk",
                    "created": _now(),
                    "model": model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            )
            done = b"data: [DONE]\n\n"
            self.wfile.write(("%x\r\n" % len(done)).encode() + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(port: int = 0) -> ThreadingHTTPServer:
    """在后台线程启动服务，返回 server（port=0 时自动分配端口）。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server._mock_thread = thread  # type: ignore[attr-defined]
    return server


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"mock OpenAI/Files server on http://127.0.0.1:{port}/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
