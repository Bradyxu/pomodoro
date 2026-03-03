#!/usr/bin/env python3
"""
本地 Web UI 服务（只使用 Python 标准库）。

架构：纯状态驱动（timestamp-based），不启动后台计时进程。
计时状态完全由 focus_state.json 中的 end_time 决定。

启动：
    python3 web.py

然后在浏览器访问：
    http://localhost:5173
"""

import json
import os
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from focus import (
    _idle_state,
    add_history_record,
    add_session,
    ensure_data_file_exists,
    load_history,
    load_state,
    save_state,
)


HOST = "localhost"
PORT = 5173

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FOCUS_SCRIPT = os.path.join(BASE_DIR, "focus.py")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")

_state_lock = threading.Lock()          # 保护 focus_state.json 的读-改-写操作

# ---- 番茄钟参数 ----
_POMO_FOCUS = 25          # 专注（分钟）
_POMO_SHORT_BREAK = 5     # 短休息
_POMO_LONG_BREAK = 15     # 长休息
_POMO_CYCLE = 4           # 每隔几轮长休息


# ---- 辅助函数 ----

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _make_running(duration_minutes, mode, **extra) -> dict:
    """构造 running 状态字典。"""
    state = {
        "status": "running",
        "pid": None,
        "start_time": _now_iso(),
        "end_time": time.time() + duration_minutes * 60,
        "duration_minutes": duration_minutes,
        "mode": mode,
    }
    state.update(extra)
    return state


# ---- 核心：完成检测与自动推进 ----

def _check_and_complete() -> dict:
    """检测计时是否到时；若到时则写入完成记录并推进状态。
    必须在 _state_lock 内调用。返回处理后的最新状态。"""
    state = load_state()

    if state.get("status") != "running":
        return state

    end_time = state.get("end_time")
    if not isinstance(end_time, (int, float)):
        save_state(_idle_state())
        return _idle_state()

    if time.time() < end_time:
        return state                                   # 还在计时

    # ---- 到时了 ----
    mode = state.get("mode", "start")

    if mode == "start":
        start_t = state.get("start_time", _now_iso())
        dur = state.get("duration_minutes", 0)
        if isinstance(dur, (int, float)) and dur > 0:
            add_session(int(round(dur)))
            add_history_record(start_t, int(round(dur)), completed=True)
        save_state(_idle_state())
        return _idle_state()

    if mode == "pomodoro":
        phase = state.get("pomodoro_phase", "focus")
        rnd   = state.get("pomodoro_round", 1)

        if phase == "focus":
            # 专注完成 → 记录 → 进入休息
            start_t = state.get("start_time", _now_iso())
            add_session(_POMO_FOCUS)
            add_history_record(start_t, _POMO_FOCUS, completed=True)

            if rnd % _POMO_CYCLE == 0:
                brk, brk_phase = _POMO_LONG_BREAK, "long_break"
            else:
                brk, brk_phase = _POMO_SHORT_BREAK, "short_break"

            ns = _make_running(brk, "pomodoro",
                               pomodoro_round=rnd, pomodoro_phase=brk_phase)
            save_state(ns)
            return ns

        else:  # short_break / long_break 结束 → 下一轮专注
            ns = _make_running(_POMO_FOCUS, "pomodoro",
                               pomodoro_round=rnd + 1, pomodoro_phase="focus")
            save_state(ns)
            return ns

    # 未知 mode → idle
    save_state(_idle_state())
    return _idle_state()


def _get_status_response() -> dict:
    """返回前端需要的标准状态字典。必须在 _state_lock 内调用。"""
    state = _check_and_complete()
    st = state.get("status", "idle")

    if st == "paused":
        remaining = state.get("remaining_seconds")
        if isinstance(remaining, (int, float)) and remaining > 0:
            return {"status": "paused", "running": False, "remaining_seconds": int(remaining)}
        save_state(_idle_state())
        return {"status": "idle", "running": False, "remaining_seconds": None}

    if st == "running":
        end_time = state.get("end_time")
        if isinstance(end_time, (int, float)):
            remaining = max(0, int(end_time - time.time()))
            if remaining > 0:
                return {"status": "running", "running": True, "remaining_seconds": remaining}
        # 到时但 _check_and_complete 可能还没处理（理论上不会走到这里）
        save_state(_idle_state())
        return {"status": "idle", "running": False, "remaining_seconds": None}

    return {"status": "idle", "running": False, "remaining_seconds": None}


# ---- 仅用于 stats（CLI 输出格式） ----

def _run_short_command(args):
    """运行一个短命令，返回 (ok, output)。"""
    try:
        completed = subprocess.run(
            args, cwd=BASE_DIR, capture_output=True, text=True, check=False,
        )
        output = completed.stdout or completed.stderr or ""
        return True, output
    except OSError as e:
        return False, f"执行命令失败：{e}"


# ---- HTTP 处理 ----

class FocusRequestHandler(BaseHTTPRequestHandler):

    def _set_json_headers(self, status_code=200):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _send_json_response(self, success: bool, data=None, error: str = None, status_code: int = 200):
        """统一发送 JSON 响应格式。"""
        try:
            resp = {"success": success, "data": data, "error": error}
            self._set_json_headers(status_code)
            self.wfile.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
        except Exception:
            try:
                self.send_error(500, "Internal server error")
            except Exception:
                pass

    def _set_html_headers(self, status_code=200):
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_OPTIONS(self):
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
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    # ---- Start ----
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

            with _state_lock:
                _check_and_complete()          # 处理可能已完成的旧计时
                save_state(_make_running(minutes, "start"))
            self._send_json_response(True, {"message": "started"})
        except Exception as e:
            self._send_json_response(False, None, f"启动失败: {str(e)}", 500)

    # ---- Pomodoro ----
    def _handle_pomodoro(self):
        try:
            with _state_lock:
                _check_and_complete()
                save_state(_make_running(
                    _POMO_FOCUS, "pomodoro",
                    pomodoro_round=1, pomodoro_phase="focus",
                ))
            self._send_json_response(True, {"message": "started"})
        except Exception as e:
            self._send_json_response(False, None, f"启动番茄钟失败: {str(e)}", 500)

    # ---- Pause ----
    def _handle_pause(self):
        try:
            with _state_lock:
                state = _check_and_complete()
                if state.get("status") != "running":
                    self._send_json_response(False, None, "当前没有正在运行的计时，无法暂停。", 400)
                    return

                end_time = state.get("end_time")
                if not isinstance(end_time, (int, float)):
                    save_state(_idle_state())
                    self._send_json_response(False, None, "状态异常，已重置。", 400)
                    return

                remaining = max(0, int(end_time - time.time()))
                save_state({
                    "status": "paused",
                    "pid": None,
                    "remaining_seconds": remaining,
                    "start_time": None,
                    "end_time": None,
                    "duration_minutes": state.get("duration_minutes"),
                    "mode": state.get("mode", "start"),
                    "pomodoro_round": state.get("pomodoro_round"),
                    "pomodoro_phase": state.get("pomodoro_phase"),
                })
            self._send_json_response(True, {"message": "paused"})
        except Exception as e:
            self._send_json_response(False, None, f"暂停失败: {str(e)}", 500)

    # ---- Resume ----
    def _handle_resume(self):
        try:
            with _state_lock:
                state = load_state()
                if state.get("status") != "paused":
                    self._send_json_response(False, None, "当前不是暂停状态，无法恢复。", 400)
                    return

                remaining = state.get("remaining_seconds")
                if not isinstance(remaining, (int, float)) or remaining <= 0:
                    save_state(_idle_state())
                    self._send_json_response(False, None, "剩余时间为 0，无法恢复。", 400)
                    return

                save_state({
                    "status": "running",
                    "pid": None,
                    "start_time": _now_iso(),
                    "end_time": time.time() + remaining,
                    "duration_minutes": state.get("duration_minutes"),
                    "mode": state.get("mode", "start"),
                    "pomodoro_round": state.get("pomodoro_round"),
                    "pomodoro_phase": state.get("pomodoro_phase"),
                })
            self._send_json_response(True, {"message": "resumed"})
        except Exception as e:
            self._send_json_response(False, None, f"恢复失败: {str(e)}", 500)

    # ---- Stop ----
    def _handle_stop(self):
        try:
            with _state_lock:
                save_state(_idle_state())
            self._send_json_response(True, {"message": "stopped"})
        except Exception as e:
            self._send_json_response(False, None, f"停止失败: {str(e)}", 500)

    # ---- Status ----
    def _handle_status(self):
        try:
            with _state_lock:
                data = _get_status_response()
            self._send_json_response(True, data)
        except Exception:
            self._send_json_response(True, {"status": "idle", "running": False, "remaining_seconds": None})

    # ---- Stats ----
    def _handle_stats(self):
        try:
            ok, output = _run_short_command(["python3", FOCUS_SCRIPT, "stats"])
            if ok:
                self._send_json_response(True, {"output": output})
            else:
                self._send_json_response(False, None, f"获取统计失败: {output}", 500)
        except Exception as e:
            self._send_json_response(False, None, f"获取统计失败: {str(e)}", 500)

    # ---- History ----
    def _handle_history(self):
        try:
            history = load_history()
            self._send_json_response(True, {"history": history if isinstance(history, list) else []})
        except Exception:
            self._send_json_response(True, {"history": []})


def run_server():
    ensure_data_file_exists()
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
