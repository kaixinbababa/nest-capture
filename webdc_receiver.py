#!/usr/bin/env python3
"""本机 WebRTC 接收端：接住页面通过 DataChannel 发回的**整段视频**（绕开 CSP / 下载限制）。

握手（文件交换，避免任何 CSP 通道）:
  1. 本脚本生成 offer → /tmp/nestdc/offer.sdp，并创建 /tmp/nestdc/ready
  2. 调用方把 offer 注入页面；页面 setRemoteDescription + createAnswer
  3. 调用方把页面返回的 answer SDP 写到 /tmp/nestdc/answer.sdp
  4. 通道打开后页面分段发视频字节；本脚本顺序追加到 /tmp/nestdc/clip.bin
   页面最后发 "DONE"。

用法: ~/.venvs/nest/bin/python webdc_receiver.py [--timeout 300]
"""
import argparse
import asyncio
import pathlib

from aiortc import RTCPeerConnection, RTCSessionDescription

WORK = pathlib.Path("/tmp/nestdc")
BLOB = WORK / "clip.bin"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()

    WORK.mkdir(parents=True, exist_ok=True)
    for f in (BLOB, WORK / "offer.sdp", WORK / "answer.sdp", WORK / "ready"):
        try:
            f.unlink()
        except OSError:
            pass

    pc = RTCPeerConnection()
    ch = pc.createDataChannel("nest")
    st = {"bytes": 0, "done": False, "texts": [], "blobtype": ""}

    @ch.on("message")
    def on_msg(msg):
        if isinstance(msg, str):
            st["texts"].append(msg)
            print(f"TEXT: {msg[:200]}", flush=True)
            if msg.startswith("BLOB "):
                st["blobtype"] = msg
            if msg.strip().startswith("DONE"):
                st["done"] = True
            return
        st["bytes"] += len(msg)
        with open(BLOB, "ab") as f:
            f.write(msg)
        if st["bytes"] % (256 * 1024) < len(msg):
            print(f"bytes: {st['bytes']}", flush=True)

    @pc.on("connectionstatechange")
    async def _():
        print("conn:", pc.connectionState, flush=True)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    for _ in range(100):
        if pc.iceGatheringState == "complete":
            break
        await asyncio.sleep(0.2)
    (WORK / "offer.sdp").write_text(pc.localDescription.sdp)
    (WORK / "ready").write_text("1")
    print("offer ready", flush=True)

    for _ in range(120):
        if (WORK / "answer.sdp").exists():
            break
        await asyncio.sleep(1)
    if not (WORK / "answer.sdp").exists():
        print("no answer sdp, abort", flush=True)
        await pc.close()
        return 2
    await pc.setRemoteDescription(RTCSessionDescription(sdp=(WORK / "answer.sdp").read_text(), type="answer"))
    print("answer set, waiting for data...", flush=True)

    waited = 0
    while waited < a.timeout and not st["done"]:
        await asyncio.sleep(0.5)
        waited += 0.5
        if pc.connectionState in ("failed", "closed"):
            print("conn ended:", pc.connectionState, flush=True)
            break
    size = BLOB.stat().st_size if BLOB.exists() else 0
    print(f"RESULT bytes={size} type={st['blobtype'][:60]} state={pc.connectionState}", flush=True)
    await pc.close()
    return 0 if size > 1000 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
