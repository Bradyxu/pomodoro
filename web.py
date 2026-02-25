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

    _save_state({"status": "idle", "pid": None, "start_time": None, "duration_minutes": None, "end_time": None, "mode": None})
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

    def _send_json_response(self, success: bool, data=None, error: str = None, status_code: int = 200):
        """统一发送 JSON 响应格式。"""
        try:
            resp = {
                "success": success,
                "data": data,
                "error": error
            }
            self._set_json_headers(status_code)
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            # 如果连发送响应都失败，记录错误但不抛出
            try:
                self.send_error(500, "Internal server error")
            except:
                pass

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
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                self._serve_index()
            elif path == "/stats":
                self._handle_stats()
            elif path == "/status":
                self._handle_status()
            elif path == "/history":
                self._handle_history()
            else:
                self._send_json_response(False, None, "Not Found", 404)
        except Exception as e:
            self._send_json_response(False, None, f"Internal error: {str(e)}", 500)

    def do_POST(self):
        try:
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
                self._send_json_response(False, None, "Not Found", 404)
        except Exception as e:
            self._send_json_response(False, None, f"Internal error: {str(e)}", 500)

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
        try:
            body = self._read_json_body()
            minutes = body.get("minutes", 25)
            try:
                minutes = int(minutes)
                if minutes <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                self._send_json_response(False, None, "minutes 必须是正整数。", 400)
                return

            state = _load_state()
            if state.get("status") in ("running", "paused"):
                _do_stop()

            remaining_seconds = minutes * 60
            ok, result = start_background_process(["python3", FOCUS_SCRIPT, "start", str(minutes)])
            if not ok:
                self._send_json_response(False, None, f"无法启动计时进程: {result}", 400)
                return

            proc = result
            now = datetime.now()
            _save_state({
                "status": "running",
                "pid": proc.pid,
                "start_time": now.isoformat(timespec="seconds"),
                "duration_minutes": minutes,
                "end_time": now.timestamp() + remaining_seconds,
                "mode": "start",
            })
            self._send_json_response(True, {"message": "started"})
        except Exception as e:
            self._send_json_response(False, None, f"启动失败: {str(e)}", 500)

    def _handle_pomodoro(self):
        try:
            state = _load_state()
            if state.get("status") in ("running", "paused"):
                _do_stop()

            ok, result = start_background_process(["python3", FOCUS_SCRIPT, "pomodoro"])
            if not ok:
                self._send_json_response(False, None, f"无法启动番茄钟: {result}", 400)
                return

            proc = result
            remaining_seconds = 25 * 60
            now = datetime.now()
            _save_state({
                "status": "running",
                "pid": proc.pid,
                "start_time": now.isoformat(timespec="seconds"),
                "duration_minutes": 25,
                "end_time": now.timestamp() + remaining_seconds,
                "mode": "pomodoro",
            })
            self._send_json_response(True, {"message": "started"})
        except Exception as e:
            self._send_json_response(False, None, f"启动番茄钟失败: {str(e)}", 500)

    def _handle_stats(self):
        try:
            ok, output = run_short_command(["python3", FOCUS_SCRIPT, "stats"])
            if ok:
                self._send_json_response(True, {"output": output})
            else:
                self._send_json_response(False, None, f"获取统计失败: {output}", 500)
        except Exception as e:
            self._send_json_response(False, None, f"获取统计失败: {str(e)}", 500)

    def _handle_history(self):
        try:
            ok, output = run_short_command(["python3", FOCUS_SCRIPT, "history"])
            if ok:
                try:
                    history = json.loads((output or "").strip())
                    if not isinstance(history, list):
                        history = []
                except json.JSONDecodeError:
                    history = []
                self._send_json_response(True, {"history": history})
            else:
                self._send_json_response(True, {"history": []})  # 失败时返回空列表
        except Exception as e:
            self._send_json_response(True, {"history": []})  # 异常时返回空列表

    def _handle_status(self):
        try:
            ok, output = run_short_command(["python3", FOCUS_SCRIPT, "status"])
            if ok:
                try:
                    data = json.loads((output or "").strip())
                    if not isinstance(data, dict):
                        data = {"status": "idle", "running": False, "remaining_seconds": None}
                except json.JSONDecodeError:
                    data = {"status": "idle", "running": False, "remaining_seconds": None}
                self._send_json_response(True, data)
            else:
                # 命令执行失败，返回 idle 状态
                self._send_json_response(True, {"status": "idle", "running": False, "remaining_seconds": None})
        except Exception as e:
            # 异常时返回 idle 状态
            self._send_json_response(True, {"status": "idle", "running": False, "remaining_seconds": None})

    def _handle_pause(self):
        try:
            ok, output = run_short_command(["python3", FOCUS_SCRIPT, "status"])
            if not ok:
                self._send_json_response(False, None, "无法获取当前状态", 500)
                return

            try:
                data = json.loads((output or "").strip())
            except json.JSONDecodeError:
                data = {}
            
            if data.get("status") != "running":
                self._send_json_response(False, None, "当前没有正在运行的计时，无法暂停。", 400)
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
                "start_time": None,
                "duration_minutes": None,
                "end_time": None,
                "mode": mode,
            })
            self._send_json_response(True, {"message": "paused"})
        except Exception as e:
            self._send_json_response(False, None, f"暂停失败: {str(e)}", 500)

    def _handle_resume(self):
        try:
            state = _load_state()
            if state.get("status") != "paused":
                self._send_json_response(False, None, "当前不是暂停状态，无法恢复。", 400)
                return

            remaining = state.get("remaining_seconds")
            if not isinstance(remaining, (int, float)) or remaining <= 0:
                _save_state({"status": "idle", "pid": None, "start_time": None, "duration_minutes": None, "end_time": None, "mode": None})
                self._send_json_response(False, None, "剩余时间为 0，无法恢复。", 400)
                return

            ok, result = start_background_process(["python3", FOCUS_SCRIPT, "start_seconds", str(int(remaining))])
            if not ok:
                self._send_json_response(False, None, f"无法恢复计时: {result}", 500)
                return

            proc = result
            now = datetime.now()
            duration_minutes = remaining / 60.0
            _save_state({
                "status": "running",
                "pid": proc.pid,
                "start_time": now.isoformat(timespec="seconds"),
                "duration_minutes": duration_minutes,
                "end_time": now.timestamp() + remaining,
                "mode": state.get("mode", "start"),
            })
            self._send_json_response(True, {"message": "resumed"})
        except Exception as e:
            self._send_json_response(False, None, f"恢复失败: {str(e)}", 500)

    def _handle_stop(self):
        try:
            _do_stop()
            self._send_json_response(True, {"message": "stopped"})
        except Exception as e:
            self._send_json_response(False, None, f"停止失败: {str(e)}", 500)


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

