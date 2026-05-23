"""商品處理中樞 HTTP API server (port 7778, P0)。

啟動:
  python -m api.server                # 預設 port 7778
  python -m api.server --port 7778
  python -m api.server --foreground    # 主執行緒跑(預設 daemon thread + 主進程 sleep)

對齊 XDZHGL 7777 的 5 個安全機制:
  1. bind 127.0.0.1 + daemon thread + try/except fail-silent
  2. api_compat_version + /api/version
  3. setdefault 模式讀寫 api/settings.json(避免 GUI 競態)
  4. X-API-Token + Idempotency-Key + Retry-After header
  5. audit log + idem db(SQLite TTL 24h),PII redact

不改 hub 既有任何 .py / .json / .xlsx。
"""
from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import secrets
import socketserver
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import API_COMPAT_VERSION, API_VERSION
from .service import HUB_DIR, health_snapshot, keyword_check_batch, auto_mapper_infer
from . import jobs as job_mgr

# ────────────────────── 路徑 / 常量 ──────────────────────

API_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = API_DIR / "settings.json"
RUNTIME_DIR = API_DIR / "runtime"
LOGS_DIR = API_DIR / "logs"
AUDIT_LOG = LOGS_DIR / "api_calls.jsonl"
IDEM_DB = RUNTIME_DIR / "api_idem.db"

DEFAULT_PORT = 7778
IDEM_TTL_SEC = 24 * 3600
WRITE_BODY_MAX = 5 * 1024 * 1024  # 5MB(支援大 batch keyword check)

_log = logging.getLogger("hub.api")
_started_at: float = 0.0
_api_token: str = ""
_started: bool = False
_httpd: Optional[socketserver.TCPServer] = None
_server_thread: Optional[threading.Thread] = None


# ────────────────────── settings.json (api/ 內,不動 hub config) ──────────────────────

def _ensure_dirs():
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _load_settings() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_settings(s: Dict[str, Any]):
    try:
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except Exception as e:
        _log.error(f"[API] save settings failed: {e}")


def _ensure_token() -> Tuple[str, int]:
    """從 api/settings.json 讀 token+port,沒有就生成寫回。回 (token, port)。

    對齊 XDZHGL setdefault 模式:即使 user 之後手動編 settings.json,
    我們不覆蓋已有值。
    """
    s = _load_settings()
    dirty = False
    tok = (s.get("api_token") or "").strip()
    if not tok:
        tok = secrets.token_urlsafe(32)
        s["api_token"] = tok
        dirty = True
    try:
        port = int(s.get("api_port") or DEFAULT_PORT)
    except Exception:
        port = DEFAULT_PORT
    if "api_port" not in s:
        s["api_port"] = port
        dirty = True
    if "api_compat_version" not in s:
        s["api_compat_version"] = API_COMPAT_VERSION
        dirty = True
    if "api_server_enabled" not in s:
        s["api_server_enabled"] = False  # 預設關,user 手動 true 才開
        dirty = True
    if dirty:
        _save_settings(s)
    return tok, port


# ────────────────────── audit log ──────────────────────

_PII_KEYS = {"api_token", "token", "password", "secret", "auth", "x-api-token"}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k.lower() in _PII_KEYS else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _audit(record: Dict[str, Any]):
    try:
        record.setdefault("ts", time.time())
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ────────────────────── idempotency DB ──────────────────────

def _ensure_idem_db():
    try:
        conn = sqlite3.connect(str(IDEM_DB), timeout=2.0)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS idem_keys ("
            "key TEXT PRIMARY KEY, response TEXT, ts REAL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON idem_keys(ts)")
        conn.execute("DELETE FROM idem_keys WHERE ts < ?",
                     (time.time() - IDEM_TTL_SEC,))
        conn.commit()
        conn.close()
    except Exception as e:
        _log.error(f"[API] idem db init failed: {e}")


def _idem_get(key: str) -> Optional[str]:
    if not key:
        return None
    try:
        conn = sqlite3.connect(str(IDEM_DB), timeout=2.0)
        cur = conn.execute("SELECT response, ts FROM idem_keys WHERE key=?", (key,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        resp, ts = row
        if time.time() - float(ts) > IDEM_TTL_SEC:
            return None
        return resp
    except Exception:
        return None


def _idem_put(key: str, response_json: str):
    if not key:
        return
    try:
        conn = sqlite3.connect(str(IDEM_DB), timeout=2.0)
        conn.execute(
            "INSERT OR REPLACE INTO idem_keys(key, response, ts) VALUES (?, ?, ?)",
            (key, response_json, time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ────────────────────── HTTP Handler ──────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return  # 不噴 stderr

    def _send_json(self, status: int, payload: Any,
                    extra_headers: Optional[Dict[str, str]] = None) -> str:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass
        return body.decode("utf-8", errors="replace")

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0 or length > WRITE_BODY_MAX:
            return {}
        try:
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _check_token(self, write_endpoint: bool = False) -> bool:
        """讀類:127.0.0.1 連線豁免;寫類:強制 token。"""
        if not write_endpoint:
            return True
        if not _api_token:
            return False
        provided = (self.headers.get("X-API-Token") or "").strip()
        if not provided:
            auth = (self.headers.get("Authorization") or "").strip()
            if auth.startswith("Bearer "):
                provided = auth[7:].strip()
        return secrets.compare_digest(provided, _api_token)

    def do_GET(self):
        try:
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            self._dispatch("GET", url.path, qs, {})
        except Exception as e:
            _log.error(f"[API] GET {self.path} crashed: {e}")
            try:
                self._send_json(500, {"error": "internal", "detail": str(e)[:200]})
            except Exception:
                pass

    def do_POST(self):
        try:
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            body = self._read_body()
            self._dispatch("POST", url.path, qs, body)
        except Exception as e:
            _log.error(f"[API] POST {self.path} crashed: {e}")
            try:
                self._send_json(500, {"error": "internal", "detail": str(e)[:200]})
            except Exception:
                pass

    def _dispatch(self, method: str, path: str, qs: Dict[str, list],
                   body: Dict[str, Any]):
        ts0 = time.time()
        status = 200
        resp_str = ""
        idem_key_used = ""
        replayed = False

        try:
            # ── 讀類(免 token) ──
            if method == "GET" and path == "/api/version":
                resp_str = self._send_json(200, {
                    "software_name": "shop_processing_hub",
                    "version": "1.0",
                    "api_version": API_VERSION,
                    "api_compat_version": API_COMPAT_VERSION,
                    "started_at": _started_at,
                    "hub_dir": str(HUB_DIR),
                })
                status = 200

            elif method == "GET" and path == "/api/health":
                snap = health_snapshot()
                snap["ok"] = True
                snap["started_at"] = _started_at
                snap["uptime_sec"] = int(time.time() - _started_at) if _started_at else 0
                snap["active_job_id"] = job_mgr.list_active().get("active_job_id")
                resp_str = self._send_json(200, snap)
                status = 200

            elif method == "GET" and path.startswith("/api/hub/etl/status/"):
                jid = path[len("/api/hub/etl/status/"):]
                st = job_mgr.get_status(jid)
                if st is None:
                    status = 404
                    resp_str = self._send_json(404, {"error": "job_id 不存在或已過期"})
                else:
                    resp_str = self._send_json(200, st)

            elif method == "GET" and path == "/api/hub/etl/jobs":
                resp_str = self._send_json(200, job_mgr.list_active())

            # ── 寫類(token + idem) ──
            elif method == "POST" and path == "/api/hub/etl/run":
                if not self._check_token(write_endpoint=True):
                    status = 401
                    resp_str = self._send_json(401, {"error": "unauthorized"})
                else:
                    idem_key_used = (body.get("idempotency_key") or "").strip()
                    if idem_key_used:
                        cached = _idem_get(idem_key_used)
                        if cached:
                            replayed = True
                            try:
                                resp_str = self._send_json(
                                    200, json.loads(cached),
                                    {"X-Idempotent-Replay": "true"})
                            except Exception:
                                resp_str = self._send_json(200, {"replay_corrupt": True})
                            return
                    # 新 job
                    input_path = (body.get("input_xlsx") or "").strip()
                    source_type = (body.get("source_type") or "").strip().lower()
                    if not input_path or source_type not in ("mercari", "goofish"):
                        status = 400
                        resp_str = self._send_json(400, {
                            "error": "input_xlsx + source_type(mercari|goofish) 必填"})
                        return
                    if not Path(input_path).exists():
                        status = 400
                        resp_str = self._send_json(400, {
                            "error": f"input_xlsx 不存在: {input_path}"})
                        return
                    out = job_mgr.submit(
                        input_path=input_path,
                        source_type=source_type,
                        output_dir=(body.get("output_dir") or "").strip(),
                        output_name=(body.get("output_name") or "").strip(),
                        steps_override=body.get("steps_override") or {},
                    )
                    if not out.get("ok"):
                        status = out.get("code", 409)
                        resp_str = self._send_json(status, out)
                    else:
                        status = 202  # accepted
                        resp_str = self._send_json(202, out)
                        if idem_key_used:
                            _idem_put(idem_key_used, resp_str)

            elif method == "POST" and path == "/api/hub/etl/cancel":
                if not self._check_token(write_endpoint=True):
                    status = 401
                    resp_str = self._send_json(401, {"error": "unauthorized"})
                else:
                    jid = (body.get("job_id") or "").strip()
                    out = job_mgr.cancel(jid)
                    status = 200 if out.get("ok") else 400
                    resp_str = self._send_json(status, out)

            elif method == "POST" and path == "/api/hub/keyword_filter/check":
                # 純讀邏輯,不需要 token(本機豁免)
                items = body.get("items") or []
                if not isinstance(items, list):
                    status = 400
                    resp_str = self._send_json(400, {"error": "items 必須是 list"})
                else:
                    status, resp = keyword_check_batch(items)
                    resp_str = self._send_json(status, resp)

            elif method == "POST" and path == "/api/hub/auto_mapper/infer":
                # 寫類(會花 GPT 額度)→ 強制 token
                if not self._check_token(write_endpoint=True):
                    status = 401
                    resp_str = self._send_json(401, {"error": "unauthorized"})
                else:
                    idem_key_used = (body.get("idempotency_key") or "").strip()
                    if idem_key_used:
                        cached = _idem_get(idem_key_used)
                        if cached:
                            replayed = True
                            try:
                                resp_str = self._send_json(
                                    200, json.loads(cached),
                                    {"X-Idempotent-Replay": "true"})
                            except Exception:
                                resp_str = self._send_json(200, {"replay_corrupt": True})
                            return
                    cat_ids = body.get("cat_ids") or []
                    if not isinstance(cat_ids, list):
                        # 也支援 items: [{category_id}]
                        items = body.get("items") or []
                        cat_ids = [it.get("category_id") for it in items
                                    if isinstance(it, dict) and it.get("category_id")]
                    threshold = body.get("threshold") or 0.0
                    try:
                        threshold = float(threshold)
                    except Exception:
                        threshold = 0.0
                    status, resp = auto_mapper_infer(cat_ids, threshold)
                    resp_str = self._send_json(status, resp)
                    if status == 200 and idem_key_used:
                        _idem_put(idem_key_used, resp_str)

            else:
                status = 404
                resp_str = self._send_json(404, {"error": "not_found", "path": path})

        finally:
            try:
                _audit({
                    "method": method,
                    "path": path,
                    "status": status,
                    "qs": _redact(dict(qs)),
                    "body": _redact(body) if method == "POST" else None,
                    "idem_key": idem_key_used or None,
                    "idem_replayed": replayed or None,
                    "client": self.client_address[0] if self.client_address else "",
                    "elapsed_ms": int((time.time() - ts0) * 1000),
                })
            except Exception:
                pass


# ────────────────────── 啟停 ──────────────────────

def start_server(port: int = DEFAULT_PORT, foreground: bool = False) -> bool:
    """啟動 server(daemon thread)。失敗只記日誌不拋。"""
    global _httpd, _server_thread, _started_at, _api_token, _started
    if _started:
        return True
    try:
        _ensure_dirs()
        _ensure_idem_db()
        _api_token, real_port = _ensure_token()
        if port == DEFAULT_PORT and real_port != DEFAULT_PORT:
            port = real_port

        # 啟動時一次性 chdir + sys.path(對齊鐵律:不每 request toggle)
        os.chdir(str(HUB_DIR))
        if str(HUB_DIR) not in sys.path:
            sys.path.insert(0, str(HUB_DIR))

        addr = ("127.0.0.1", int(port))
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        _httpd = socketserver.ThreadingTCPServer(addr, _Handler)
        _httpd.daemon_threads = True
        _started_at = time.time()
        _started = True

        if foreground:
            _log.info(f"[API] server bound 127.0.0.1:{port} (foreground)")
            print(f"[API] 商品處理中樞 API listening on http://127.0.0.1:{port}")
            print(f"[API] settings: {SETTINGS_PATH}")
            print(f"[API] token:    (saved in settings.json, X-API-Token required for write)")
            try:
                _httpd.serve_forever()
            except KeyboardInterrupt:
                pass
        else:
            _server_thread = threading.Thread(
                target=_httpd.serve_forever, daemon=True, name="hub-api")
            _server_thread.start()
            _log.info(f"[API] server bound 127.0.0.1:{port}")
        return True
    except Exception as e:
        _log.error(f"[API] start failed: {e}")
        _started = False
        return False


def stop_server():
    global _httpd, _started
    try:
        if _httpd is not None:
            _httpd.shutdown()
            _httpd.server_close()
            _httpd = None
        _started = False
    except Exception as e:
        _log.error(f"[API] stop failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="商品處理中樞 API server (port 7778)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--background", action="store_true",
                        help="背景跑(預設前景)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(message)s")

    if args.background:
        start_server(args.port, foreground=False)
        # 主執行緒 sleep 等中斷
        try:
            while _started:
                time.sleep(60)
        except KeyboardInterrupt:
            stop_server()
    else:
        start_server(args.port, foreground=True)


if __name__ == "__main__":
    main()
