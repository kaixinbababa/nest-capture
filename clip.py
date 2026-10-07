#!/usr/bin/env python3
"""事件 → 抓帧 → 20 秒小视频（1 fps 延时片）→ 可选推送。

用法:
    ~/.venvs/nest/bin/python clip.py "DOORBELL" --send --caption "👤 有人 · DOORBELL"

流程:
    1) 用 Chrome 扩展打开该相机实时画面（新标签，结束自动关闭）
    2) 每隔约 5 秒截一帧，共 --frames 帧
    3) PyAV 编码成 --seconds 秒的 mp4（1 fps 延时感）
    4) --send 时通过 openclaw message 发到 Telegram

依赖: Chrome 常开且保持登录（截图走扩展）；~/.venvs/nest 内有 PyAV。
"""
import os
import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import time

HOME = pathlib.Path.home()
OPENCLAW = os.environ.get("OPENCLAW_BIN", "openclaw")
STATE = HOME / ".local" / "state" / "nest-events"
CLIPS = STATE / "clips"
# openclaw message 只允许从 media 目录发本机文件，所以发之前先拷过去
MEDIA_DIR = HOME / ".openclaw" / "media" / "outbound"
HERE = pathlib.Path(__file__).resolve().parent
MAPFILE = HERE / "gh-cameras.json"
MAKE_CLIP = HERE / "make_clip.py"
GH_BASE = os.environ.get("NEST_GH_BASE", "https://home.google.com/u/0/home/<your-home-id>")
TARGET = os.environ.get("NEST_TG_DM", "<your-telegram-user-id>")
KEEP_FRAME_DIRS = 3


def run(cmd, timeout=90):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def shot_path(out):
    """CLI 打印的是 ~/... 路径，取最后一行 .png 并展开 ~"""
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.endswith(".png"):
            return line.replace("~", str(HOME), 1) if line.startswith("~") else line
    return None


def cleanup_old_frames():
    dirs = sorted([d for d in CLIPS.glob("frames-*") if d.is_dir()], key=lambda d: d.name)
    for d in dirs[:-KEEP_FRAME_DIRS]:
        shutil.rmtree(d, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("camera")
    ap.add_argument("--frames", type=int, default=20)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--caption", default="")
    ap.add_argument("--interval", type=float, default=0.3)
    a = ap.parse_args()

    cmap = json.loads(MAPFILE.read_text()) if MAPFILE.exists() else {}
    seg = (cmap.get(a.camera) or cmap.get(a.camera.lower()) or cmap.get("_default") or "")
    url = f"{GH_BASE}/cameras/list/{seg}" if seg else f"{GH_BASE}/cameras/grid"

    CLIPS.mkdir(parents=True, exist_ok=True)
    label = f"nestclip{int(time.time()) % 100000}"
    print(f"[clip] camera={a.camera} → {url}", flush=True)

    rc, out, err = run([OPENCLAW, "browser", "--browser-profile", "chrome",
                        "open", url, "--label", label], timeout=90)
    if rc != 0:
        print(f"[clip] open failed rc={rc} {err[:200]}", flush=True)
        return 1
    time.sleep(8)  # 等实时画面起来

    frames = CLIPS / f"frames-{int(time.time())}"
    frames.mkdir(parents=True, exist_ok=True)
    got = 0
    for i in range(1, a.frames + 1):
        rc, out, err = run([OPENCLAW, "browser", "--browser-profile", "chrome",
                            "screenshot", label], timeout=90)
        p = shot_path(out)
        if p and pathlib.Path(p).exists():
            shutil.copy(p, frames / f"f{i:02d}.png")
            got += 1
        time.sleep(a.interval)
    print(f"[clip] captured {got}/{a.frames} frames", flush=True)
    run([OPENCLAW, "browser", "--browser-profile", "chrome", "close", label], timeout=60)
    cleanup_old_frames()

    if got < 3:
        print("[clip] too few frames, abort", flush=True)
        return 1

    mp4 = CLIPS / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.camera.replace(' ', '_')}.mp4"
    rc, out, err = run([sys.executable, str(MAKE_CLIP), str(frames), str(mp4),
                        "--seconds", str(a.seconds)], timeout=300)
    print("[clip]", out or err, flush=True)
    if rc != 0 or not mp4.exists():
        return 1

    if a.send:
        caption = time.strftime("🕒 %Y-%m-%d %H:%M:%S %Z\n") + (a.caption or f"🎥 {a.camera} · {a.seconds:.0f}s")
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        send_path = MEDIA_DIR / mp4.name
        shutil.copy(mp4, send_path)
        rc, out, err = run([OPENCLAW, "message", "send", "--channel", "telegram",
                            "--target", TARGET, "--message", caption,
                            "--media", str(send_path), "--force-document"], timeout=180)
        print(f"[clip] send rc={rc} {out[:120] or err[:120]}", flush=True)
        if rc != 0:
            return 1
    else:
        print(f"[clip] saved {mp4}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
