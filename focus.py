#!/usr/bin/env python3
"""
简单的终端专注计时器。

用法：
    python3 focus.py start 5     # 开始 5 分钟专注
    python3 focus.py stats       # 查看今日专注统计
    python3 focus.py pomodoro   # 番茄钟模式（25/5，四轮后 15 分钟长休息）
"""

import json
import os
import sys
import time
from datetime import datetime, date


def get_data_file_path() -> str:
    """返回 focus.json 的绝对路径（和脚本放在同一目录）。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "focus.json")


def get_state_file_path() -> str:
    """返回计时状态文件的绝对路径。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "focus_state.json")


def get_history_file_path() -> str:
    """返回专注历史文件的绝对路径。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "focus_history.json")


def ensure_data_file_exists() -> None:
    """如果数据文件不存在，则创建一个空的 JSON 列表。"""
    path = get_data_file_path()
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)
        except OSError:
            # 如果创建失败，后续读取时会再处理
            pass


def load_records():
    """从 JSON 文件加载所有记录，失败时返回空列表。"""
    path = get_data_file_path()
    if not os.path.exists(path):
        return []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        # 如果不是列表，就视为损坏，忽略内容
        return []
    except (json.JSONDecodeError, OSError):
        # JSON 损坏或读取失败时，视为无记录
        return []


def save_records(records) -> None:
    """将所有记录写回 JSON 文件。"""
    path = get_data_file_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except OSError:
        print("写入数据文件失败，请检查磁盘权限。", file=sys.stderr)


def load_state():
    """读取 focus_state.json，唯一状态源。"""
    path = get_state_file_path()
    if not os.path.exists(path):
        return _idle_state()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return _idle_state()
    except (OSError, json.JSONDecodeError):
        return _idle_state()


def save_state(state: dict) -> None:
    """写入 focus_state.json。"""
    path = get_state_file_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _idle_state() -> dict:
    return {
        "status": "idle",
        "pid": None,
        "start_time": None,
        "duration_minutes": None,
        "end_time": None,
        "mode": None,
    }


def _write_running_state(pid: int, duration_minutes: float, mode: str) -> None:
    """写入运行状态，基于时间戳计算。
    
    :param duration_minutes: 持续时间（分钟），可以是整数或浮点数
    """
    now = datetime.now()
    start_time = now.isoformat(timespec="seconds")
    end_time = now.timestamp() + duration_minutes * 60
    save_state({
        "status": "running",
        "pid": pid,
        "start_time": start_time,
        "duration_minutes": duration_minutes,
        "end_time": end_time,
        "mode": mode,
    })


def _write_idle_state() -> None:
    save_state(_idle_state())


def load_history():
    """加载专注历史记录。"""
    path = get_history_file_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (OSError, json.JSONDecodeError):
        return []


def save_history(history: list) -> None:
    """保存专注历史记录。"""
    path = get_history_file_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def add_history_record(start_time: str, duration_minutes: int, completed: bool) -> None:
    """添加一条专注历史记录。"""
    history = load_history()
    record = {
        "start_time": start_time,
        "duration_minutes": duration_minutes,
        "completed": completed,
    }
    history.append(record)
    save_history(history)


def add_session(minutes: int) -> None:
    """新增一条专注记录。"""
    records = load_records()
    now = datetime.now()
    record = {
        "timestamp": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),  # 例如：2026-02-18
        "minutes": minutes,
    }
    records.append(record)
    save_records(records)


def run_timer(minutes: int, propagate_interrupt: bool = False) -> None:
    """运行专注倒计时，结束后保存记录。

    :param minutes: 专注分钟数
    :param propagate_interrupt: 为 True 时，Ctrl+C 会继续向外抛出，方便上层统一处理。
    """
    total_seconds = minutes * 60
    start_time = datetime.now().isoformat(timespec="seconds")

    print(f"开始专注 {minutes} 分钟。按 Ctrl+C 可中途结束（不会记录本次专注）。")

    _write_running_state(os.getpid(), minutes, "start")

    try:
        for remaining in range(total_seconds, 0, -1):
            m, s = divmod(remaining, 60)
            sys.stdout.write(f"\r剩余时间：{m:02d}:{s:02d}")
            sys.stdout.flush()
            time.sleep(1)
        print()
    except KeyboardInterrupt:
        print("\n已中断本次专注，未保存记录。")
        if propagate_interrupt:
            _write_idle_state()
            raise
        _write_idle_state()
        return

    _write_idle_state()
    add_session(minutes)
    add_history_record(start_time, minutes, completed=True)
    print(f"⏰ 专注 {minutes} 分钟结束！干得好！")


def run_timer_seconds(seconds: int, propagate_interrupt: bool = False) -> None:
    """按秒数运行专注倒计时（用于 Resume）。完成后按秒数折算分钟记录。"""
    if seconds <= 0:
        return

    start_time = datetime.now().isoformat(timespec="seconds")
    # 将秒数转换为分钟（向上取整，至少1分钟）
    duration_minutes = max(1, (seconds + 59) // 60)

    print(f"继续专注 {seconds} 秒。按 Ctrl+C 可中途结束。")

    # 计算实际的持续时间（分钟），用于状态存储
    # 但实际计时仍按秒数进行
    actual_duration_minutes = seconds / 60.0  # 精确的分钟数
    _write_running_state(os.getpid(), actual_duration_minutes, "start")

    try:
        for remaining in range(seconds, 0, -1):
            m, s = divmod(remaining, 60)
            sys.stdout.write(f"\r剩余时间：{m:02d}:{s:02d}")
            sys.stdout.flush()
            time.sleep(1)
        print()
    except KeyboardInterrupt:
        print("\n已中断本次专注，未保存记录。")
        if propagate_interrupt:
            _write_idle_state()
            raise
        _write_idle_state()
        return

    _write_idle_state()
    add_session(duration_minutes)
    add_history_record(start_time, duration_minutes, completed=True)
    print(f"⏰ 专注结束！干得好！")


def run_break(minutes: int, is_long: bool = False, propagate_interrupt: bool = False) -> None:
    """运行休息倒计时，不记录到专注统计中。"""
    label = "长休息" if is_long else "休息"

    print(f"开始{label} {minutes} 分钟。按 Ctrl+C 可中途结束。")

    _write_running_state(os.getpid(), minutes, "pomodoro")

    total_seconds = minutes * 60
    try:
        for remaining in range(total_seconds, 0, -1):
            m, s = divmod(remaining, 60)
            sys.stdout.write(f"\r{label}剩余时间：{m:02d}:{s:02d}")
            sys.stdout.flush()
            time.sleep(1)
        print()
    except KeyboardInterrupt:
        print(f"\n已中断本次{label}。")
        if propagate_interrupt:
            _write_idle_state()
            raise
        _write_idle_state()
        return

    _write_idle_state()
    print(f"{label}结束，准备继续加油！")


def run_pomodoro() -> None:
    """番茄钟模式：25 分钟专注 + 5 分钟休息，每 4 轮后 15 分钟长休息，自动循环。"""
    focus_minutes = 25
    short_break_minutes = 5
    long_break_minutes = 15

    print("番茄钟模式：")
    print(f"- 每轮：专注 {focus_minutes} 分钟 + 休息 {short_break_minutes} 分钟")
    print(f"- 每完成 4 轮后：长休息 {long_break_minutes} 分钟")
    print("按 Ctrl+C 可随时结束番茄钟模式。\n")

    round_no = 1

    try:
        while True:
            print(f"====== 第 {round_no} 轮 ======")
            print(f"第 {round_no} 轮：开始专注 {focus_minutes} 分钟")
            # 番茄钟中的专注也计入专注统计
            run_timer(focus_minutes, propagate_interrupt=True)

            # 判断是短休息还是长休息
            if round_no % 4 == 0:
                break_minutes = long_break_minutes
                is_long = True
            else:
                break_minutes = short_break_minutes
                is_long = False

            print(f"第 {round_no} 轮：开始{'长休息' if is_long else '休息'} {break_minutes} 分钟")
            run_break(break_minutes, is_long=is_long, propagate_interrupt=True)

            round_no += 1
            print()
    except KeyboardInterrupt:
        print("\n已退出番茄钟模式。")


def show_stats() -> None:
    """显示今日专注统计。"""
    today_str = date.today().isoformat()
    records = load_records()
    today_records = [r for r in records if r.get("date") == today_str]

    count = len(today_records)
    total_minutes = sum(int(r.get("minutes", 0)) for r in today_records)

    print("====== 今日专注统计 ======")
    print(f"日期：{today_str}")
    print(f"专注次数：{count} 次")
    print(f"总时长：{total_minutes} 分钟")

    if count == 0:
        print()
        print("今天还没有专注记录。")
        print("可以试试：python3 focus.py start 5")


def _is_process_alive(pid) -> bool:
    """检查进程是否存活。"""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def get_status_dict():
    """返回当前计时状态的字典，基于时间戳动态计算剩余时间。"""
    state = load_state()
    status = state.get("status", "idle")

    if status == "idle":
        return {"status": "idle", "running": False, "remaining_seconds": None}

    if status == "paused":
        # 暂停状态：使用保存的剩余秒数
        remaining = state.get("remaining_seconds")
        if isinstance(remaining, (int, float)) and remaining > 0:
            return {"status": "paused", "running": False, "remaining_seconds": int(remaining)}
        save_state(_idle_state())
        return {"status": "idle", "running": False, "remaining_seconds": None}

    if status == "running":
        pid = state.get("pid")
        if not _is_process_alive(pid):
            save_state(_idle_state())
            return {"status": "idle", "running": False, "remaining_seconds": None}

        # 基于时间戳计算剩余时间
        end_time = state.get("end_time")
        if not isinstance(end_time, (int, float)):
            save_state(_idle_state())
            return {"status": "idle", "running": False, "remaining_seconds": None}

        now = time.time()
        remaining = max(0, int(end_time - now))

        if remaining <= 0:
            save_state(_idle_state())
            return {"status": "idle", "running": False, "remaining_seconds": None}

        return {"status": "running", "running": True, "remaining_seconds": remaining}

    save_state(_idle_state())
    return {"status": "idle", "running": False, "remaining_seconds": None}


def print_usage() -> None:
    """打印简单的使用说明。"""
    print("用法：")
    print("  python3 focus.py start <分钟数>      开始一次专注计时")
    print("  python3 focus.py start_seconds <秒>  按秒数开始专注（用于 Resume）")
    print("  python3 focus.py stats               查看今日专注统计")
    print("  python3 focus.py pomodoro             番茄钟模式")
    print("  python3 focus.py status              查看当前计时状态（JSON）")
    print()
    print("示例：")
    print("  python3 focus.py start 5")
    print("  python3 focus.py stats")


def main(argv=None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    ensure_data_file_exists()

    if not argv:
        print("缺少命令参数。")
        print_usage()
        return

    command = argv[0]

    if command == "start":
        if len(argv) < 2:
            print("请在 start 后面加上专注的分钟数，例如：python3 focus.py start 5")
            return
        try:
            minutes = int(argv[1])
            if minutes <= 0:
                raise ValueError
        except ValueError:
            print("分钟数必须是正整数，例如：python3 focus.py start 5")
            return
        run_timer(minutes)
    elif command == "start_seconds":
        if len(argv) < 2:
            print("请在 start_seconds 后面加上秒数，例如：python3 focus.py start_seconds 120")
            return
        try:
            seconds = int(argv[1])
            if seconds <= 0:
                raise ValueError
        except ValueError:
            print("秒数必须是正整数，例如：python3 focus.py start_seconds 120")
            return
        run_timer_seconds(seconds)
    elif command == "stats":
        show_stats()
    elif command == "pomodoro":
        run_pomodoro()
    elif command == "status":
        status_info = get_status_dict()
        # 只输出 JSON，方便 Web 端解析
        print(json.dumps(status_info, ensure_ascii=False))
    elif command == "history":
        history = load_history()
        # 只输出 JSON，方便 Web 端解析
        print(json.dumps(history, ensure_ascii=False))
    else:
        print(f"未知命令：{command}")
        print_usage()


if __name__ == "__main__":
    main()

