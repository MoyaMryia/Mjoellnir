#!/usr/bin/env python3
"""Mock OpenAI Chat Completions 服务：按剧本驱动 cpi 的工具循环测试。

用法:
  mock_llm.py [--host 127.0.0.1] [--port 18080] [--script turns.json]
              [--default-reply "（mock 无更多剧本）"] [--loop] [--dump-dir DIR]

端点:
  POST /v1/chat/completions   （任意以 /chat/completions 结尾的路径均可）
  GET  /health

剧本 JSON（turns 数组；每次请求按顺序取一条，取完回 default-reply）:
{
  "turns": [
    {"tool_calls": [{"name": "find_tools", "arguments": {"query": ["图书馆"]}}]},
    {"tool_calls": [{"name": "bash", "arguments": {"cmd": "echo hi"}}]},
    {"reasoning": "工具跑完了", "content": "最终答案"}
  ]
}

每条 turn 可选字段:
  content        回答文本（按 chunk_size 切片流式发送）
  reasoning      reasoning_content 文本（思考流）
  tool_calls     [{"name": "...", "arguments": dict|str}]（arguments 为 dict 时自动 JSON 编码）
  finish_reason  默认: 有 tool_calls -> "tool_calls"，否则 "stop"（可用 "length" 测配额截断）
  chunk_size     SSE 分片字符数（默认 8）
  delay_ms       响应前延迟（测打断）
  break_after    发 N 个分片后直接断开（测断流 69，不发 [DONE]）
  http_status    非 200 时直接返回该状态码（测上游不可用/凭据被拒）

说明: 非流式请求丢弃 reasoning（OpenAI 非流式无此字段）；请求体可经 --dump-dir 落盘备查。
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def die(msg, code=2):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def split_chunks(s, n):
    return [s[i:i + n] for i in range(0, len(s), n)] or [""]


class Mock:
    def __init__(self, turns, default_reply, loop):
        self.turns = turns
        self.default_reply = default_reply
        self.loop = loop
        self.idx = 0
        self.lock = threading.Lock()

    def next_turn(self):
        with self.lock:
            if self.idx < len(self.turns):
                t = self.turns[self.idx]
                self.idx += 1
                return t, self.idx
            if self.loop and self.turns:
                self.idx = 1
                return self.turns[0], 1
            return {"content": self.default_reply}, self.idx


class Handler(BaseHTTPRequestHandler):
    server_version = "cpi-mock/0.1"
    mock = None
    dump_dir = ""
    req_no = 0
    req_lock = threading.Lock()

    def log_message(self, fmt, *args):  # 关掉默认 access log，自己打
        pass

    # ---------- 基础设施 ----------

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log(self, msg):
        print(f"[mock] {msg}", file=sys.stderr, flush=True)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    # ---------- 响应构造 ----------

    @staticmethod
    def _norm_args(arguments):
        if isinstance(arguments, str):
            return arguments
        return json.dumps(arguments, ensure_ascii=False)

    @staticmethod
    def _deltas(turn):
        size = max(1, int(turn.get("chunk_size") or 8))
        out = []
        if turn.get("reasoning"):
            out += [{"reasoning_content": c} for c in split_chunks(str(turn["reasoning"]), size)]
        if turn.get("content"):
            out += [{"content": c} for c in split_chunks(str(turn["content"]), size)]
        for i, call in enumerate(turn.get("tool_calls") or []):
            args = Handler._norm_args(call.get("arguments", ""))
            parts = split_chunks(args, size)
            out.append({"tool_calls": [{"index": i, "id": f"call_{i}", "type": "function",
                                        "function": {"name": call.get("name") or "?",
                                                     "arguments": parts[0]}}]})
            for p in parts[1:]:
                out.append({"tool_calls": [{"index": i, "function": {"arguments": p}}]})
        return out

    def _stream(self, turn, model):
        deltas = self._deltas(turn)
        finish = turn.get("finish_reason") or ("tool_calls" if turn.get("tool_calls") else "stop")
        brk = turn.get("break_after")
        cid = f"chatcmpl-mock-{int(time.time() * 1000)}"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(delta, fr):
            payload = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": fr}]}
            self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        for i, d in enumerate(deltas):
            if brk is not None and i >= int(brk):
                self._log(f"break_after={brk}: 断开流（不发 [DONE]）")
                return
            emit(d, None)
        emit({}, finish)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _whole(self, turn, model):
        calls = []
        for i, call in enumerate(turn.get("tool_calls") or []):
            calls.append({"id": f"call_{i}", "type": "function",
                          "function": {"name": call.get("name") or "?",
                                       "arguments": self._norm_args(call.get("arguments", ""))}})
        msg = {"role": "assistant", "content": turn.get("content") or ""}
        if calls:
            msg["tool_calls"] = calls
        finish = turn.get("finish_reason") or ("tool_calls" if calls else "stop")
        self._send_json(200, {
            "id": "chatcmpl-mock", "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    # ---------- 主入口 ----------

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json(404, {"error": {"message": "path must end with /chat/completions"}})
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            req = json.loads(raw.decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"error": {"message": f"bad json: {e}"}})
            return

        with Handler.req_lock:
            Handler.req_no += 1
            no = Handler.req_no
        if self.dump_dir:
            path = os.path.join(self.dump_dir, f"req-{no:04d}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(req, f, ensure_ascii=False, indent=2)

        msgs = req.get("messages") or []
        last = msgs[-1] if msgs else {}
        preview = str(last.get("content", ""))[:80].replace("\n", " ")
        self._log(f"#{no} model={req.get('model')} stream={bool(req.get('stream'))} "
                  f"msgs={len(msgs)} tools={len(req.get('tools') or [])} "
                  f"last={last.get('role')}:{preview!r}")

        turn, served = self.mock.next_turn()
        self._log(f"#{no} 剧本 #{served}/{len(self.mock.turns)} "
                  f"content={bool(turn.get('content'))} calls={len(turn.get('tool_calls') or [])} "
                  f"finish={turn.get('finish_reason') or '(auto)'}")

        status = int(turn.get("http_status") or 200)
        if status != 200:
            self._send_json(status, {"error": {"message": turn.get("error_message", "mock error")}})
            return
        if turn.get("delay_ms"):
            time.sleep(int(turn["delay_ms"]) / 1000.0)
        if req.get("stream"):
            self._stream(turn, req.get("model") or "mock")
        else:
            self._whole(turn, req.get("model") or "mock")


def main():
    ap = argparse.ArgumentParser(description="Mock OpenAI Chat Completions（cpi 测试用）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--script", default="", help="剧本 JSON 文件（turns 数组或 {\"turns\":[...]}）")
    ap.add_argument("--default-reply", default="（mock 无更多剧本）")
    ap.add_argument("--loop", action="store_true", help="剧本用完后从头循环")
    ap.add_argument("--dump-dir", default="", help="把每个请求体落盘到该目录")
    args = ap.parse_args()

    turns = []
    if args.script:
        try:
            with open(args.script, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as e:
            die(f"读取剧本 {args.script}: {e}")
        turns = raw.get("turns") if isinstance(raw, dict) else raw
        if not isinstance(turns, list):
            die("剧本必须是 turns 数组或 {\"turns\": [...]}")
    if args.dump_dir:
        os.makedirs(args.dump_dir, exist_ok=True)

    Handler.mock = Mock(turns, args.default_reply, args.loop)
    Handler.dump_dir = args.dump_dir
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[mock] listening http://{args.host}:{args.port}/v1/chat/completions "
          f"turns={len(turns)} default={args.default_reply!r}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
