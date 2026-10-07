#!/usr/bin/env python3
"""Nest SDM 实时取帧：GenerateWebRtcStream + aiortc → 存一张 JPEG。

用法:
    ~/.venvs/nest/bin/python sdm_frame.py <device-id|设备名> <输出.jpg> [--timeout 25]

为什么需要这个：这些摄像头只支持 WEB_RTC，Google 拒绝 CameraEventImage.GenerateImage
（"camera not supporting RTSP protocol"），所以事件拿不到事件图 —— 只能临时拉一条
WebRTC 流，抓一帧当"现场照片"。

依赖: aiortc（已装在 ~/.venvs/nest）+ ~/.config/nest-sdm/*
"""
import asyncio
import json
import pathlib
import sys
import time

import requests
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription

CONF = pathlib.Path.home() / ".config" / "nest-sdm"
SDM = "https://smartdevicemanagement.googleapis.com/v1"
NAMES_FILE = pathlib.Path(__file__).resolve().parent / "device-names.json"


def token():
    c = json.loads((CONF / "client.json").read_text())
    t = json.loads((CONF / "token.json").read_text())
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": c["client_id"], "client_secret": c["client_secret"],
        "refresh_token": t["refresh_token"], "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return c["project_id"], r.json()["access_token"]


def _dev_name(d):
    """有 customName 就用它，否则退回设备类型名（如 DOORBELL / CAMERA）。"""
    return (d.get("traits", {}).get("sdm.devices.traits.Info", {}).get("customName")
            or d["type"].split(".")[-1])


def resolve_device(headers, pid, want):
    """want 可以是 device id 或设备名（大小写不敏感）"""
    r = requests.get(f"{SDM}/enterprises/{pid}/devices", headers=headers, timeout=30)
    r.raise_for_status()
    devs = r.json().get("devices", [])
    wl = want.lower()
    for d in devs:
        did = d["name"].split("/")[-1]
        name = _dev_name(d)
        if wl == did.lower() or wl == name.lower():
            return did, name
    # 模糊匹配
    for d in devs:
        did = d["name"].split("/")[-1]
        name = _dev_name(d)
        if wl in name.lower() or wl in did.lower():
            return did, name
    raise SystemExit(f"device not found: {want}")


def sdm_command(pid, headers, device_id, command, params):
    r = requests.post(f"{SDM}/enterprises/{pid}/devices/{device_id}:executeCommand",
                      headers=headers, timeout=30,
                      json={"command": command, "params": params})
    return r


def sanitize_sdp(sdp: str) -> str:
    """Google 的 answer SDP 里 candidate 行缺 foundation 字段
    （如 `a=candidate: 1 udp ...`），aiortc 解析会报
    ValueError: invalid literal for int() with base 10: 'udp'。
    这里补一个占位 foundation。"""
    out = []
    for line in sdp.replace("\r\n", "\n").split("\n"):
        if line.startswith("a=candidate:") and line[len("a=candidate:"):].startswith(" "):
            line = "a=candidate:1" + line[len("a=candidate:"):]
        out.append(line)
    return "\r\n".join(out) + "\r\n"


async def grab(pid, headers, device_id, out_path, timeout=25):
    pc = RTCPeerConnection()
    got = asyncio.Event()
    saved = {}

    @pc.on("track")
    def on_track(track):
        if track.kind != "video":
            return

        async def consume():
            while not got.is_set():
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=timeout)
                except Exception as e:
                    print(f"  track ended: {type(e).__name__}", flush=True)
                    return
                if getattr(frame, "width", 0) and getattr(frame, "height", 0):
                    img = frame.to_image()
                    img.save(out_path, quality=88)
                    saved.update(w=frame.width, h=frame.height)
                    got.set()
                    return

        asyncio.ensure_future(consume())

    # SDM 要求 offer 里 m=audio / m=video / m=application 三条，且必须按这个顺序
    pc.addTransceiver("audio", direction="recvonly")
    pc.addTransceiver("video", direction="recvonly")
    pc.createDataChannel("data")  # 产生 m=application 行
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    for _ in range(60):
        if pc.iceGatheringState == "complete":
            break
        await asyncio.sleep(0.2)
    offer_sdp = pc.localDescription.sdp

    r = sdm_command(pid, headers, device_id,
                    "sdm.devices.commands.CameraLiveStream.GenerateWebRtcStream",
                    {"offerSdp": offer_sdp})
    if r.status_code != 200:
        await pc.close()
        raise SystemExit(f"GenerateWebRtcStream {r.status_code}: {r.text[:300]}")
    res = r.json()["results"]
    media_session_id = res.get("mediaSessionId")
    await pc.setRemoteDescription(RTCSessionDescription(sdp=sanitize_sdp(res["answerSdp"]), type="answer"))

    try:
        await asyncio.wait_for(got.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    await pc.close()

    if media_session_id:
        sdm_command(pid, headers, device_id,
                    "sdm.devices.commands.CameraLiveStream.StopWebRtcStream",
                    {"mediaSessionId": media_session_id})
    return saved


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    want, out = sys.argv[1], pathlib.Path(sys.argv[2])
    timeout = 25
    if "--timeout" in sys.argv:
        timeout = int(sys.argv[sys.argv.index("--timeout") + 1])

    pid, at = token()
    headers = {"Authorization": f"Bearer {at}"}
    device_id, name = resolve_device(headers, pid, want)
    print(f"device: {name} ({device_id[:16]}…) → {out}", flush=True)
    t0 = time.time()
    saved = asyncio.run(grab(pid, headers, device_id, out, timeout))
    if out.exists() and out.stat().st_size > 0:
        print(f"OK {saved} {out.stat().st_size}B in {time.time()-t0:.1f}s", flush=True)
        return 0
    print(f"FAIL no frame in {time.time()-t0:.1f}s", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
