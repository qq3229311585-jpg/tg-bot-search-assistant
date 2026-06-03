#!/usr/bin/env python3
"""ask_server.py — HTTP /ask 接口（供 OpenHuman 等外部系统调用）"""

import json, hashlib, os, logging, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from tg_bot.config import ASK_API_PORT, ASK_TOKEN_FILE, DATA_DIR
from tg_bot.file_io import atomic_write_text

log = logging.getLogger(__name__)

_ask_lock = threading.Lock()  # 同一时间只让一个 HTTP 请求跑流水线，避免文件竞态

ASK_API_TOKEN = None  # main() 启动时填充


def _load_or_create_ask_token():
    try:
        with open(ASK_TOKEN_FILE) as f:
            tok = f.read().strip()
            if tok:
                return tok
    except Exception:
        pass
    tok = hashlib.sha256(os.urandom(32)).hexdigest()
    try:
        atomic_write_text(ASK_TOKEN_FILE, tok, mode=0o600)
    except Exception as e:
        log.warning(f"无法保存 ASK_TOKEN：{e}")
    return tok


class _AskHandler(BaseHTTPRequestHandler):
    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/health", "/"):
            self._json(200, {"ok": True, "service": "tg-bot /ask"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/ask":
            self._json(404, {"error": "use POST /ask"})
            return
        # 鉴权
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:].strip() != ASK_API_TOKEN:
            self._json(401, {"error": "invalid bearer token"})
            return
        # 读取请求体
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body   = self.rfile.read(length).decode("utf-8")
            data   = json.loads(body) if body else {}
            query  = (data.get("query") or "").strip()
            brief  = bool(data.get("brief", False))
        except Exception as e:
            self._json(400, {"error": f"bad request body: {e}"})
            return
        if not query:
            self._json(400, {"error": "missing query"})
            return
        # 跑流水线（延迟导入避免循环依赖）
        from tg_bot.bot import handle
        with _ask_lock:
            log.info(f"📥 HTTP /ask: {query[:60]}")
            try:
                reply = handle(chat_id=0, text=query, http_mode=True, brief=brief)
                if reply is None:
                    reply = ""
                self._json(200, {"reply": reply, "ok": True})
            except Exception as e:
                log.error(f"HTTP /ask 流水线失败: {e}", exc_info=True)
                self._json(500, {"error": str(e), "ok": False})

    def log_message(self, fmt, *args):
        # 静默 BaseHTTPRequestHandler 默认日志（避免和我们的 log 重复）
        return


def _run_ask_server():
    try:
        srv = HTTPServer(("0.0.0.0", ASK_API_PORT), _AskHandler)
        log.info(f"🌐 HTTP /ask 接口监听 0.0.0.0:{ASK_API_PORT}（Bearer Token 鉴权）")
        srv.serve_forever()
    except Exception as e:
        log.error(f"HTTP /ask 服务启动失败: {e}")
