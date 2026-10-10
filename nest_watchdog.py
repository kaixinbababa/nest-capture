#!/usr/bin/env python3
"""Nest 链路看门狗（由 nest-watchdog.timer 定期拉起）。

检查顺序：**先环境、后守护**。

  ① 环境：默认 profile 的 Chrome（带 OpenClaw 扩展）是否在跑、相机标签页是否在。
     判据 = gateway 的 `browser --browser-profile chrome status` 里的 running 字段。
     ★ 绝不能用 `pgrep -c chrome` —— 沙箱 profile 的 Chrome 会被算进去 → 假阳性
       → 2026-10-07~10 正是因此断链 3 天无人自愈。
     ★ `tabs` 返回 0 也不足为证（实测扩展掉线时它照样成功返回）→ 必须解析 running 字段
       并核对相机标签页标签是否都在。
  ② 环境健康后，才检查预录守护的 ready 是否新鲜；不新鲜才重启该守护
     （环境坏着的时候反复重启守护是无效噪音）。
  ③ 拉起 Chrome 后【必须等它真的连上】再宣布成功；连不上就发限频告警。
"""
import os
import pathlib
import re
import subprocess
import sys
import time

HOME = pathlib.Path.home()
STATE = HOME / ".local" / "state" / "nest-events"
LOG = STATE / "watchdog.log"
OPENCLAW = os.environ.get("OPENCLAW_BIN", "openclaw")
STALE_SEC = 120            # ready 标记多久算不新鲜
ALERT_MIN_INTERVAL = 3600  # 告警限频（秒）
CHROME_WAIT_SEC = 75       # 拉起 Chrome 后最多等多久确认连上
RELCHROME_MIN_INTERVAL = 600  # 拉起 Chrome 的限频
TARGET = os.environ.get("NEST_TG_TARGET", "<your-telegram-chat-id>")
THREAD_ID = os.environ.get("NEST_TG_THREAD", "<your-thread-id>")
CAMERAS = {"Garage camera": "nest-warm-garage", "DOORBELL": "nest-warm-doorbell"}
TAB_LABELS = ["nestwarmGarage_cam", "nestwarmDOORBELL"]


def log(msg):
    STATE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {msg}\n")


def slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def chrome_running():
    """默认 profile 的 Chrome（扩展宿主）是否在跑 —— 以 gateway 判定为准。"""
    rc, out, err = run([OPENCLAW, "browser", "--browser-profile", "chrome", "status"], timeout=30)
    txt = out + err
    m = re.search(r"^running:\s*(true|false)", txt, re.M | re.I)
    return (m is not None and m.group(1).lower() == "true"), txt.strip().replace("\n", " ")[:220]


def camera_tabs_ok():
    """相机标签页是否都在（running 为真时才有意义）。"""
    rc, out, err = run([OPENCLAW, "browser", "--browser-profile", "chrome", "tabs"], timeout=30)
    txt = out + err
    missing = [l for l in TAB_LABELS if l not in txt]
    return (rc == 0 and not missing), missing


def relaunch_chrome():
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    subprocess.Popen(["setsid", "/usr/bin/google-chrome-stable"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, start_new_session=True, env=env)


def alert(msg):
    stamp = STATE / "watchdog-alert.stamp"
    last = stamp.stat().st_mtime if stamp.exists() else 0
    if time.time() - last <= ALERT_MIN_INTERVAL:
        return False
    rc, out, err = run([OPENCLAW, "message", "send", "--channel", "telegram",
                        "--target", TARGET, "--thread-id", THREAD_ID, "--message", msg], timeout=60)
    if rc == 0:
        stamp.write_text(str(int(time.time())))
        return True
    log(f"[告警] 发送失败 rc={rc} {err[:80]}")
    return False


def main():
    running, raw = chrome_running()
    if running:
        tabs_ok, missing = camera_tabs_ok()
    else:
        tabs_ok, missing = False, TAB_LABELS
    log(f"[环境] chrome running={running} tabs_ok={tabs_ok}" + (f" missing={missing}" if missing else ""))

    if not (running and tabs_ok):
        stamp = STATE / "watchdog-chrome.stamp"
        last = stamp.stat().st_mtime if stamp.exists() else 0
        if time.time() - last <= RELCHROME_MIN_INTERVAL:
            log(f"[Chrome] 异常但处于限频窗口（{int(time.time() - last)}s 前刚拉过）")
            return 0
        log(f"[Chrome] 环境异常（running={running} tabs_ok={tabs_ok}）→ 拉起默认 Chrome")
        relaunch_chrome()
        stamp.write_text(str(int(time.time())))
        t0 = time.time()
        ok = False
        while time.time() - t0 < CHROME_WAIT_SEC:
            time.sleep(5)
            r2, _ = chrome_running()
            if r2:
                ok = True
                break
        if ok:
            log("[Chrome] 已连上扩展 → 重启两个预录守护")
            for _, unit in CAMERAS.items():
                run(["systemctl", "--user", "restart", f"{unit}.service"], timeout=60)
            alert("ℹ️ Nest 链路：默认 Chrome 曾掉线，看门狗已自动拉起并恢复，视频恢复。")
        else:
            log("[Chrome] ⚠️ 拉起后仍未连上（扩展或登录态可能有问题）")
            alert("⚠️ Nest 链路故障：默认 Chrome 掉线，看门狗已尝试拉起但【未连上扩展】。"
                  "视频会持续中断 —— 需要你在这台机器上手动看一眼 Chrome / OpenClaw 扩展。")
        return 0

    # 环境健康 → 只处理真正卡住的守护
    restarted = []
    for cam, unit in CAMERAS.items():
        ready = STATE / f"warm-{slug(cam)}.ready"
        age = (time.time() - ready.stat().st_mtime) if ready.exists() else 10 ** 6
        if age > STALE_SEC:
            log(f"[守护] {cam} ready 不新鲜（{int(age)}s）→ 重启 {unit}")
            run(["systemctl", "--user", "restart", f"{unit}.service"], timeout=60)
            restarted.append(cam)
    log(f"[完成] 环境=ok · 重启={','.join(restarted) or '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
