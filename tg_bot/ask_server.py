#!/usr/bin/env python3
"""Small, dependency-free HTTP API for Telegram bot integrations."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer as HTTPServer
from urllib.parse import urlsplit

from tg_bot.config import (
    ASK_API_HOST,
    ASK_API_MAX_BODY_BYTES,
    ASK_API_MAX_QUERY_CHARS,
    ASK_API_PORT,
    ASK_API_RATE_LIMIT,
    ASK_API_RATE_WINDOW_SECONDS,
    ASK_API_TOKEN as CONFIG_API_TOKEN,
    ASK_API_TRUST_PROXY,
    ASK_TOKEN_FILE,
    ensure_data_dir,
    validate_config,
)
from tg_bot.file_io import atomic_write_text

log = logging.getLogger(__name__)

API_VERSION = "v1"
SERVICE_VERSION = os.getenv("TG_BOT_VERSION", "1.3.0")
_ask_lock = threading.Lock()  # pipeline remains single-flight for file safety
_rate_lock = threading.Lock()
_rate_state: dict[str, tuple[float, int]] = {}

ASK_API_TOKEN = CONFIG_API_TOKEN or None  # main() fills this when using a file token


def _load_or_create_ask_token():
    """Use an explicit token when provided, otherwise persist a random token."""
    if CONFIG_API_TOKEN:
        return CONFIG_API_TOKEN
    try:
        ensure_data_dir()
        with open(ASK_TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                return tok
    except Exception:
        pass
    tok = hashlib.sha256(os.urandom(32)).hexdigest()
    try:
        ensure_data_dir()
        atomic_write_text(ASK_TOKEN_FILE, tok, mode=0o600)
    except Exception as e:
        log.warning("无法保存 ASK_TOKEN：%s", e)
    return tok


def _request_id(handler: BaseHTTPRequestHandler) -> str:
    """Return a safe client request id or generate one."""
    candidate = (handler.headers.get("X-Request-ID") or "").strip()
    if 0 < len(candidate) <= 128 and all(
        ch.isalnum() or ch in "._:-" for ch in candidate
    ):
        return candidate
    return uuid.uuid4().hex


def _client_identity(handler: BaseHTTPRequestHandler) -> str:
    """Use forwarded identity only when explicitly trusted by deployment config."""
    if ASK_API_TRUST_PROXY:
        forwarded = (handler.headers.get("X-Forwarded-For") or "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded[:128]
    return (handler.client_address[0] if handler.client_address else "unknown")[:128]


def _check_rate_limit(identity: str) -> bool:
    """Apply a small in-process fixed-window limit; zero disables it."""
    if ASK_API_RATE_LIMIT <= 0:
        return True
    now = time.monotonic()
    with _rate_lock:
        start, count = _rate_state.get(identity, (now, 0))
        if now - start >= ASK_API_RATE_WINDOW_SECONDS:
            start, count = now, 0
        count += 1
        _rate_state[identity] = (start, count)
        if len(_rate_state) > 2048:
            cutoff = now - ASK_API_RATE_WINDOW_SECONDS
            for key, (window_start, _count) in list(_rate_state.items()):
                if window_start < cutoff:
                    _rate_state.pop(key, None)
        return count <= ASK_API_RATE_LIMIT


def _expected_token() -> str:
    return (ASK_API_TOKEN or CONFIG_API_TOKEN or "").strip()


def _authenticate(handler: BaseHTTPRequestHandler) -> bool:
    expected = _expected_token()
    if not expected:
        return False
    bearer = handler.headers.get("Authorization", "")
    if bearer.startswith("Bearer "):
        supplied = bearer[7:].strip()
    else:
        supplied = (handler.headers.get("X-API-Key") or "").strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _payload(request_id: str, **values):
    return {"api_version": API_VERSION, "request_id": request_id, **values}


class _AskHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, error, request_id, *, details=None):
        values = {"ok": False, "error": error}
        if details is not None:
            values["details"] = details
        self._json(status, _payload(request_id, **values))

    def do_GET(self):
        request_id = _request_id(self)
        path = urlsplit(self.path).path
        if path in ("/", "/health"):
            self._json(200, _payload(request_id, ok=True, service="tg-bot /ask"))
            return
        if path == "/readyz":
            diagnostics = validate_config()
            if diagnostics["ok"]:
                try:
                    ensure_data_dir()
                except OSError as exc:
                    diagnostics = {
                        **diagnostics,
                        "ok": False,
                        "errors": [f"数据目录不可用：{exc}"],
                    }
            if diagnostics["ok"]:
                self._json(200, _payload(request_id, ok=True, details=diagnostics))
            else:
                self._error(503, "not_ready", request_id, details=diagnostics)
            return
        if path == "/version":
            self._json(200, _payload(request_id, ok=True, version=SERVICE_VERSION))
            return
        if path == "/capabilities":
            self._json(
                200,
                _payload(
                    request_id,
                    ok=True,
                    endpoints=[
                        "/ask",
                        "/v1/ask",
                        "/health",
                        "/readyz",
                        "/version",
                        "/capabilities",
                    ],
                    methods={"ask": ["POST"], "health": ["GET"]},
                ),
            )
            return
        self._error(404, "not_found", request_id)

    def do_POST(self):
        request_id = _request_id(self)
        path = urlsplit(self.path).path
        if path not in ("/ask", "/v1/ask"):
            self._error(404, "not_found", request_id)
            return
        if not _check_rate_limit(_client_identity(self)):
            self._error(429, "rate_limited", request_id)
            return
        if not _authenticate(self):
            self._error(401, "invalid_token", request_id)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(400, "invalid_content_length", request_id)
            return
        if length < 0 or length > ASK_API_MAX_BODY_BYTES:
            self._error(413, "request_too_large", request_id)
            return
        try:
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body) if body else {}
        except Exception as exc:
            self._error(400, "invalid_json", request_id, details=str(exc))
            return
        if not isinstance(data, dict):
            self._error(400, "invalid_json", request_id)
            return
        query = (data.get("query") or "").strip()
        brief = bool(data.get("brief", False))
        if not query:
            self._error(400, "missing_query", request_id)
            return
        if len(query) > ASK_API_MAX_QUERY_CHARS:
            self._error(413, "query_too_large", request_id)
            return
        from tg_bot.bot import handle

        with _ask_lock:
            log.info("📥 HTTP %s: %s", path, query[:60])
            try:
                reply = handle(chat_id=0, text=query, http_mode=True, brief=brief) or ""
                self._json(200, _payload(request_id, reply=reply, ok=True))
            except Exception as exc:
                log.error("HTTP %s 流水线失败: %s", path, exc, exc_info=True)
                self._error(500, "pipeline_failed", request_id, details=str(exc))

    def log_message(self, fmt, *args):
        # 静默 BaseHTTPRequestHandler 默认日志（避免和我们的 log 重复）
        return


def _run_ask_server():
    try:
        srv = HTTPServer((ASK_API_HOST, ASK_API_PORT), _AskHandler)
        log.info("🌐 HTTP API 监听 %s:%s（/ask 与 /v1/ask）", ASK_API_HOST, ASK_API_PORT)
        srv.serve_forever()
    except Exception as e:
        log.error("HTTP /ask 服务启动失败: %s", e)
