#!/usr/bin/env python3
"""Nest SDM 事件守听：Pub/Sub pull → 解析事件 → 抓事件图片 → 发到 Telegram。

用法:
    setsid nohup ~/.venvs/nest/bin/python -u sdm_events.py > /tmp/nest-events/listener.log 2>&1 < /dev/null &

产物:
    /tmp/nest-events/events.jsonl     每行一个事件
    /tmp/nest-events/img/*.jpg        事件图片（门铃/有人/有动静/声音）

依赖: ~/.config/nest-sdm/{client.json,token.json}（token 需含 sdm.service + pubsub）
"""
import base64
import json
import pathlib
import subprocess
import sys
import time

import requests

CONF = pathlib.Path.home() / ".config" / "nest-sdm"
GCP = "openclaw-agent-yan"
SUB = f"projects/{GCP}/subscriptions/nest-events-sub"
SDM = "https://smartdevicemanagement.googleapis.com/v1"
PUBSUB = "https://pubsub.googleapis.com/v1"
# 持久化目录（/tmp 重启会清空）：~/.local/state/nest-events
OUT = pathlib.Path.home() / ".local" / "state" / "nest-events"
IMG = OUT / "img"
IMG_KEEP_DAYS = 7

OPENCLAW = os.environ.get("OPENCLAW_BIN", "openclaw")
CHANNEL = "telegram"
# 所有 Nest 事件/视频只推到这个论坛群的「🪺 Nest 事件」话题，不再打扰私聊
TARGET = os.environ.get("NEST_TG_TARGET", "<your-telegram-chat-id>")
THREAD_ID = os.environ.get("NEST_TG_THREAD", "<your-thread-id>")

LABELS = {
    "sdm.devices.events.CameraPerson.Person": ("👤 看到人", "person"),
    "sdm.devices.events.CameraMotion.Motion": ("🏃 有动静", "motion"),
    "sdm.devices.events.CameraSound.Sound": ("🔊 有声音", "sound"),
    "sdm.devices.events.DoorbellChime.Chime": ("🔔 有人按门铃", "chime"),
}
HERE = pathlib.Path(__file__).resolve().parent
NAMES_FILE = HERE / "device-names.json"
CLIP_PY = HERE / "clip.py"
SMOOTH_PY = HERE / "smooth_capture.py"  # 流畅录制 + 视觉审片（有事件时用这条）
CLIP_LOCK = OUT / "clip.lock"

# —— 筛选：只推"真安全事件" ——
# ① 类型：只认"有人"和"门铃被按"；motion（车流/树影/光影）与 sound 一律不推
NOTIFY_EVENTS = (
    "sdm.devices.events.CameraPerson.Person",
    "sdm.devices.events.DoorbellChime.Chime",
)
# ② 相机：只推这些（门口那台门铃）。想把车库/后院加进来，把名字写进元组即可
NOTIFY_CAMERAS = ("DOORBELL", "Garage camera")
# ③ 合并：同一相机在冷却期内连续触发的事件只推第一条
NOTIFY_COOLDOWN_SEC = 120
CLIP_EVENTS = NOTIFY_EVENTS  # 出片的类型与推送保持一致
_last_notify = {}


def token():
    c = json.loads((CONF / "client.json").read_text())
    t = json.loads((CONF / "token.json").read_text())
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return c["project_id"], r.json()["access_token"]


def load_names():
    if NAMES_FILE.exists():
        return json.loads(NAMES_FILE.read_text())
    return {}


def refresh_names(headers, pid, cache):
    try:
        r = requests.get(f"{SDM}/enterprises/{pid}/devices", headers=headers, timeout=30)
        if r.status_code == 200:
            for d in r.json().get("devices", []):
                did = d["name"].split("/")[-1]
                nm = d.get("traits", {}).get("sdm.devices.traits.Info", {}).get("customName")
                cache[did] = nm or d["type"].split(".")[-1]
            NAMES_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"name refresh failed: {e}", flush=True)
    return cache


def notify(text, image=None):
    cmd = [OPENCLAW, "message", "send", "--channel", CHANNEL, "--target", TARGET,
           "--thread-id", THREAD_ID, "--message", text]
    if image:
        cmd += ["--media", str(image)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=90)
        if p.returncode != 0:
            print(f"notify failed rc={p.returncode}: {p.stderr.decode()[:200]}", flush=True)
    except Exception as e:
        print(f"notify error: {type(e).__name__}: {e}", flush=True)


def maybe_start_clip(device, label_text):
    """事件后异步抓帧合成 20 秒小视频（同一时间只跑一个）。"""
    if CLIP_LOCK.exists():
        try:
            info = json.loads(CLIP_LOCK.read_text())
            pid_ = int(info.get("pid") or 0)
            started = float(info.get("at") or 0)
            if pid_ and pathlib.Path(f"/proc/{pid_}").exists():
                age = (time.time() - started) if started else 0
                if age > 240:
                    # 卡死的抓拍进程：不清掉会把后续事件全部挡在门外（今晚 18:23 后就是这个）
                    print(f"[clip] 旧抓拍卡死 {int(age)}s (pid {pid_}) → 清锁并终止", flush=True)
                    try:
                        import os as _os
                        import signal as _sig
                        _os.kill(pid_, _sig.SIGTERM)
                    except Exception:
                        pass
                    try:
                        CLIP_LOCK.unlink()
                    except OSError:
                        pass
                else:
                    print(f"[clip] already running (pid {pid_}), skip", flush=True)
                    return False
        except Exception:
            pass
    # 优先走常驻预录：事件前 ~60 秒画面已在守护手里（不必再等 50 秒才开录）
    try:
        import re as _re
        ws = _re.sub(r"[^A-Za-z0-9]+", "_", device).strip("_")
        wpf = OUT / f"warm-{ws}.pid"
        rdf = OUT / f"warm-{ws}.ready"
        fresh = rdf.exists() and (time.time() - rdf.stat().st_mtime) < 60
        if wpf.exists() and fresh:
            wpid = int((wpf.read_text().strip() or "0"))
            if wpid and pathlib.Path(f"/proc/{wpid}").exists():
                (OUT / f"warm-{ws}.trigger").write_text(str(time.time()), encoding="utf-8")
                print(f"[clip] → 预录触发 {device} (warm pid {wpid})", flush=True)
                return True
            why = f"守护进程 {wpid} 不存在"
        else:
            why = f"pid文件={wpf.exists()} ready={rdf.exists()} fresh={fresh}"
        print(f"[clip] 预录不可用（{why}）→ 回退旧流程", flush=True)
    except Exception as e:
        print(f"[clip] 预录判断异常（回退旧流程）: {type(e).__name__}: {str(e)[:90]}", flush=True)
    try:
        log = open(OUT / "capture.log", "a")
        p = subprocess.Popen(
            [sys.executable, str(SMOOTH_PY), device, "--seconds", "20",
             "--judge", "--auto-send", "--send", "--person", "--caption", label_text],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except Exception as e:
        print(f"[clip] spawn failed: {type(e).__name__}: {e}", flush=True)
        return False
    try:
        CLIP_LOCK.write_text(json.dumps({"pid": p.pid, "at": time.time(), "device": device}))
    except Exception:
        pass
    print(f"[clip] started pid={p.pid} for {device}", flush=True)
    return True


def fetch_image(headers, pid, device_id, event_id, tag):
    r = requests.post(f"{SDM}/enterprises/{pid}/devices/{device_id}:executeCommand",
                      headers=headers, timeout=30, json={
                          "command": "sdm.devices.commands.CameraEventImage.GenerateImage",
                          "params": {"eventId": event_id}})
    if r.status_code != 200:
        return None, f"GenerateImage {r.status_code}: {r.text[:160]}"
    img = requests.get(r.json()["results"]["url"], timeout=60)
    IMG.mkdir(parents=True, exist_ok=True)
    fn = IMG / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.jpg"
    fn.write_bytes(img.content)
    return fn, None


def prune_images(days=IMG_KEEP_DAYS):
    """清掉 N 天前的事件图片，避免无限堆积。"""
    if not IMG.exists():
        return 0
    cutoff = time.time() - days * 86400
    n = 0
    for f in IMG.glob("*.jpg"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"pruned {prune_images()} old image(s)", flush=True)
    pid, at = token()
    headers = {"Authorization": f"Bearer {at}"}
    names = load_names()
    names = refresh_names(headers, pid, names)
    print(f"[{time.strftime('%H:%M:%S')}] listener up · project={pid} · events={len(names)} devices", flush=True)

    refresh_at = time.time() + 2400
    names_at = time.time() + 3600
    fails = 0
    while True:
        try:
            if time.time() > refresh_at:
                pid, at = token()
                headers = {"Authorization": f"Bearer {at}"}
                refresh_at = time.time() + 2400
            if time.time() > names_at:
                names = refresh_names(headers, pid, names)
                names_at = time.time() + 3600

            r = requests.post(f"{PUBSUB}/{SUB}:pull", headers=headers,
                              json={"maxMessages": 10}, timeout=90)
            if r.status_code != 200:
                fails += 1
                print(f"pull {r.status_code}: {r.text[:200]}", flush=True)
                time.sleep(min(60, 5 * fails))
                continue
            fails = 0
            msgs = r.json().get("receivedMessages", [])
            ack = []
            for m in msgs:
                ack.append(m["ackId"])
                try:
                    ev = json.loads(base64.b64decode(m["message"]["data"]).decode())
                except Exception:
                    continue
                if "pubsubTopicPermissionCheck" in ev:
                    continue
                ru = ev.get("resourceUpdate") or {}
                device_id = (ru.get("name") or "").split("/")[-1]
                dev = names.get(device_id, device_id[:12] if device_id else "?")
                for name, body in (ru.get("events") or {}).items():
                    label, tag = LABELS.get(name, (name.split(".")[-1], "event"))
                    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "device": dev,
                           "deviceId": device_id, "event": name, "eventId": ev.get("eventId"),
                           "ts": ev.get("timestamp")}
                    # ① 类型筛选：车流/树影（motion）、噪音（sound）只入库不打扰
                    if name not in NOTIFY_EVENTS:
                        rec["filtered"] = "type"
                    # ② 相机筛选：只保留门口那台
                    elif NOTIFY_CAMERAS and dev not in NOTIFY_CAMERAS:
                        rec["filtered"] = "camera"
                    # ③ 合并：同一相机冷却期内的连续事件只推一次
                    elif time.time() - _last_notify.get(dev, 0) < NOTIFY_COOLDOWN_SEC:
                        rec["filtered"] = "cooldown"
                    else:
                        _last_notify[dev] = time.time()
                        eid = (body or {}).get("eventId")
                        clip_started = False
                        if name in CLIP_EVENTS:
                            clip_started = maybe_start_clip(dev, f"🎥 {label} · {dev}")
                            rec["clip"] = clip_started
                        hint = "\n🔍 正在抓拍并评估，值得看的会自动推视频" if clip_started else ""
                        if eid:
                            path, err = fetch_image(headers, pid, device_id, eid, tag)
                            rec["image"] = str(path) if path else None
                            if err:
                                rec["imageError"] = err
                            notify(f"🚨 {label} · {dev}{hint}", path)
                        else:
                            notify(f"🚨 {label} · {dev}{hint}")
                    with (OUT / "events.jsonl").open("a") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(f"[{time.strftime('%H:%M:%S')}] {json.dumps(rec, ensure_ascii=False)}", flush=True)
            if ack:
                requests.post(f"{PUBSUB}/{SUB}:acknowledge", headers=headers,
                              json={"ackIds": ack}, timeout=30)
        except KeyboardInterrupt:
            print("bye", flush=True)
            return
        except Exception as e:
            fails += 1
            print(f"loop error: {type(e).__name__}: {e}", flush=True)
            time.sleep(min(60, 5 * fails))


if __name__ == "__main__":
    main()
