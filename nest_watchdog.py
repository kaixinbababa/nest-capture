#!/usr/bin/env python3
"""Nest 链路看门狗（每 5 分钟一次）。

检查两件事，都是今天真实踩过的坑：
  ① 预录守护的 ready 标记是否新鲜 —— 不新鲜就重启该守护
     （今天失败模式：中继超时 → 守护注入 rc=1 → ready 停更 15 小时无人发现）
  ② 浏览器中继是否响应（tabs 调用）—— 不响应就记日志 + 限频告警一次
     （今天失败模式：createTab 超时 → 抓拍开不了页 → 零视频）

用法: nest_watchdog.py（由 nest-watchdog.timer 每 5 分钟拉起）
"""
import pathlib
import re
import subprocess
import sys
import time

HOME = pathlib.Path.home()
STATE = HOME / ".local" / "state" / "nest-events"
LOG = STATE / "watchdog.log"
STALE_SEC = 120           # ready 标记超过这个时间算不新鲜
ALERT_MIN_INTERVAL = 3600  # 告警限频（秒）
TARGET = os.environ.get("NEST_TG_TARGET", "<your-telegram-chat-id>")
THREAD_ID = os.environ.get("NEST_TG_THREAD", "<your-thread-id>")
CAMERAS = {"Garage camera": "nest-warm-garage", "DOORBELL": "nest-warm-doorbell"}


def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")


def slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def chrome_alive():
    try:
        out = subprocess.run(["pgrep", "-c", "chrome"], capture_output=True, text=True, timeout=10)
        return int((out.stdout or "0").strip() or "0") > 0
    except Exception:
        return False


def relaunch_chrome():
    import os
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    subprocess.Popen(["setsid", "/usr/bin/google-chrome-stable"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True, env=env)


def main():
    restarted, relay_ok = [], None
    for cam, unit in CAMERAS.items():
        ready = STATE / f"warm-{slug(cam)}.ready"
        age = (time.time() - ready.stat().st_mtime) if ready.exists() else 10 ** 6
        if age > STALE_SEC:
            log(f"[守护] {cam} ready 不新鲜（{int(age)}s）→ 重启 {unit}")
            try:
                subprocess.run(["systemctl", "--user", "restart", f"{unit}.service"], timeout=60)
                restarted.append(cam)
            except Exception as e:
                log(f"[守护] 重启 {unit} 失败: {type(e).__name__}: {str(e)[:80]}")
    try:
        p = subprocess.run(["openclaw", "browser", "--browser-profile", "chrome", "tabs"],
                           capture_output=True, text=True, timeout=25)
        relay_ok = p.returncode == 0
    except Exception:
        relay_ok = False
    if relay_ok is False or not chrome_alive():
        stamp = STATE / "watchdog-chrome.stamp"
        last = stamp.stat().st_mtime if stamp.exists() else 0
        if time.time() - last > 600:
            log(f"[Chrome] 未运行或中继无响应（chrome_alive={chrome_alive()} relay={relay_ok}）→ 重新拉起")
            try:
                relaunch_chrome()
                stamp.write_text(str(int(time.time())))
                time.sleep(25)
                for cam, unit in CAMERAS.items():
                    subprocess.run(["systemctl", "--user", "restart", f"{unit}.service"], timeout=60)
                log("[Chrome] 已拉起并重启守护")
            except Exception as e:
                log(f"[Chrome] 拉起失败: {type(e).__name__}: {str(e)[:80]}")
        else:
            log(f"[Chrome] 异常但处于限频窗口（{int(time.time() - last)}s 前刚拉过）")
    if relay_ok is False:
        log("[中继] 浏览器中继无响应（tabs 失败）")
        stamp = STATE / "watchdog-alert.stamp"
        last = stamp.stat().st_mtime if stamp.exists() else 0
        if time.time() - last > ALERT_MIN_INTERVAL:
            try:
                subprocess.run(["openclaw", "message", "send", "--channel", "telegram",
                                "--target", TARGET, "--thread-id", THREAD_ID,
                                "--message", ("⚠️ Nest 摄像头链路异常：浏览器中继无响应，"
                                              "抓拍/视频会停。我已记录，会持续重试；"
                                              "若持续不下，需要重启一次 Chrome。")], timeout=60)
                stamp.write_text(str(int(time.time())))
                log("[中继] 已发限频告警")
            except Exception as e:
                log(f"[中继] 告警发送失败: {type(e).__name__}: {str(e)[:80]}")
    log(f"[完成] 重启={','.join(restarted) or '无'} · 中继={'ok' if relay_ok else 'fail'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
