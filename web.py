#!/usr/bin/env python3
"""
本地 Web UI 服务（只使用 Python 标准库）。

启动：
    python3 web.py

然后在浏览器访问：
    http://localhost:5173
"""

import json
import os
import signal
import subprocess
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse


HOST = "localhost"
PORT = 5173

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOCUS_SCRIPT = os.path.join(BASE_DIR, "focus.py")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")
STATE_FILE = os.path.join(BASE_DIR, "focus_state.json")

_current_process = None
_process_lock = threading.Lock()


def _load_state():
    """读取 focus_state.json。"""
    if not os.path.exists(STATE_FILE):
        return {"status": "idle", "pid": None, "remaining_seconds": None}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {"status": "idle", "pid": None, "remaining_seconds": None}
    except (OSError, json.JSONDecodeError):
        return {"status": "idle", "pid": None, "remaining_seconds": None}


def _save_state(state: dict) -> None:
    """写入 focus_state.json。"""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _kill_process_group(pid: int) -> bool:
    """彻底杀掉进程组（mac/linux）。"""
    if pid is None:
        return False
    try:
        if os.name != "nt":
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, ProcessLookupError):
        try:
            if os.name != "nt":
                os.killpg(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        return False


def _do_stop() -> str:
    """停止计时：杀进程并清空状态。返回消息。"""
    global _current_process
    state = _load_state()
    pid = state.get("pid")
    with _process_lock:
        proc = _current_process
        if proc is not None and proc.poll() is None:
            _kill_process_group(proc.pid)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _kill_process_group(proc.pid)
            _current_process = None
        elif pid is not None:
            _kill_process_group(pid)
            _current_process = None

    _save_state({"status": "idle", "pid": None, "remaining_seconds": None, "started_at": None, "mode": None})
    with _process_lock:
        _current_process = None
    return "stopped"


def start_background_process(args, use_setsid: bool = True):
    """启动一个后台进程。use_setsid 为 True 时，子进程在独立进程组，便于彻底 kill。"""
    global _current_process
    kwargs = {
        "args": args,
        "cwd": BASE_DIR,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if use_setsid and os.name != "nt":
        kwargs["preexec_fn"] = os.setsid
    try:
        proc = subprocess.Popen(**kwargs)
        with _process_lock:
            _current_process = proc
        return True, proc
    except OSError as e:
        return False, str(e)


def run_short_command(args):
    """运行一个短命令，用于 stats 之类的接口，返回 (ok, output)。"""
    try:
        completed = subprocess.run(
            args,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        output = completed.stdout or completed.stderr or ""
        return True, output
    except OSError as e:
        return False, f"执行命令失败：{e}"


class FocusRequestHandler(BaseHTTPRequestHandler):
    def _set_json_headers(self, status_code=200):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _set_html_headers(self, status_code=200):
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_OPTIONS(self):
        # 简单的 CORS 处理（目前前端同源，这里只是为了完整性）
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self._serve_index()
        elif path == "/stats":
            self._handle_stats()
        elif path == "/status":
            self._handle_status()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/start":
            self._handle_start()
        elif path == "/pomodoro":
            self._handle_pomodoro()
        elif path == "/pause":
            self._handle_pause()
        elif path == "/resume":
            self._handle_resume()
        elif path == "/stop":
            self._handle_stop()
        else:
            self.send_error(404, "Not Found")

    # ---- 具体处理函数 ----

    def _serve_index(self):
        try:
            with open(INDEX_FILE, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            self.send_error(500, "index.html not found or unreadable")
            return

        self._set_html_headers(200)
        self.wfile.write(content.encode("utf-8"))

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, dict):
                return data
            return {}
        except json.JSONDecodeError:
            return {}

    def _handle_start(self):
        body = self._read_json_body()
        minutes = body.get("minutes", 25)
        try:
            minutes = int(minutes)
            if minutes <= 0:
                raise ValueError
        except (TypeError, ValueError):
            self._set_json_headers(400)
            resp = {"ok": False, "message": "minutes 必须是正整数。"}
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        state = _load_state()
        if state.get("status") in ("running", "paused"):
            _do_stop()

        remaining_seconds = minutes * 60
        ok, result = start_background_process(["python3", FOCUS_SCRIPT, "start", str(minutes)])
        if not ok:
            self._set_json_headers(400)
            resp = {"ok": False, "message": result}
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        proc = result
        _save_state({
            "status": "running",
            "pid": proc.pid,
            "remaining_seconds": remaining_seconds,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "total_seconds": remaining_seconds,
            "mode": "start",
        })
        self._set_json_headers(200)
        resp = {"ok": True, "message": "started"}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def _handle_pomodoro(self):
        state = _load_state()
        if state.get("status") in ("running", "paused"):
            _do_stop()

        ok, result = start_background_process(["python3", FOCUS_SCRIPT, "pomodoro"])
        if not ok:
            self._set_json_headers(400)
            resp = {"ok": False, "message": result}
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        proc = result
        remaining_seconds = 25 * 60
        _save_state({
            "status": "running",
            "pid": proc.pid,
            "remaining_seconds": remaining_seconds,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "total_seconds": remaining_seconds,
            "mode": "pomodoro",
        })
        self._set_json_headers(200)
        resp = {"ok": True, "message": "started"}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def _handle_stats(self):
        ok, output = run_short_command(["python3", FOCUS_SCRIPT, "stats"])
        status = 200 if ok else 500
        self._set_json_headers(status)
        resp = {"ok": ok, "output": output}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def _handle_status(self):
        ok, output = run_short_command(["python3", FOCUS_SCRIPT, "status"])
        if ok:
            try:
                data = json.loads((output or "").strip())
            except json.JSONDecodeError:
                data = {"status": "idle", "running": False, "remaining_seconds": None}
        else:
            data = {"status": "idle", "running": False, "remaining_seconds": None, "error": output}
        self._set_json_headers(200)
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _handle_pause(self):
        ok, output = run_short_command(["python3", FOCUS_SCRIPT, "status"])
        try:
            data = json.loads((output or "").strip())
        except json.JSONDecodeError:
            data = {}
        if data.get("status") != "running":
            self._set_json_headers(400)
            resp = {"ok": False, "message": "当前没有正在运行的计时，无法暂停。"}
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        remaining = data.get("remaining_seconds")
        if not isinstance(remaining, (int, float)) or remaining < 0:
            remaining = 0

        state_before = _load_state()
        mode = state_before.get("mode")
        _do_stop()
        _save_state({
            "status": "paused",
            "pid": None,
            "remaining_seconds": int(remaining),
            "started_at": None,
            "mode": mode,
        })
        self._set_json_headers(200)
        resp = {"ok": True, "message": "paused"}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def _handle_resume(self):
        state = _load_state()
        if state.get("status") != "paused":
            self._set_json_headers(400)
            resp = {"ok": False, "message": "当前不是暂停状态，无法恢复。"}
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        remaining = state.get("remaining_seconds")
        if not isinstance(remaining, (int, float)) or remaining <= 0:
            _save_state({"status": "idle", "pid": None, "remaining_seconds": None, "started_at": None, "mode": None})
            self._set_json_headers(400)
            resp = {"ok": False, "message": "剩余时间为 0，无法恢复。"}
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        ok, result = start_background_process(["python3", FOCUS_SCRIPT, "start_seconds", str(int(remaining))])
        if not ok:
            self._set_json_headers(500)
            resp = {"ok": False, "message": result}
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
            return

        proc = result
        _save_state({
            "status": "running",
            "pid": proc.pid,
            "remaining_seconds": int(remaining),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "total_seconds": int(remaining),
            "mode": state.get("mode", "start"),
        })
        self._set_json_headers(200)
        resp = {"ok": True, "message": "resumed"}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))

    def _handle_stop(self):
        _do_stop()
        self._set_json_headers(200)
        resp = {"ok": True, "message": "stopped"}
        self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))


def run_server():
    server_address = (HOST, PORT)
    httpd = HTTPServer(server_address, FocusRequestHandler)
    print(f"Web UI 服务已启动：http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止服务器。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务器...")
    finally:
        httpd.server_close()
        print("服务器已停止。")


if __name__ == "__main__":
    run_server()

