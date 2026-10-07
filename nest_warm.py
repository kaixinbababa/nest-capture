#!/usr/bin/env python3
"""常驻预录守护（解决"抓拍比事件晚 50 秒"）。

原理：
  相机页**常开**，页面里用 MediaRecorder 对**正在播放的原生流**连续录制（每段 10 秒），
  在内存里滚动保留最近 N 段（默认 6 段 = 60 秒）—— 也就是**事件发生前的画面一直在手上**。
  事件来了 → listener 写触发文件 → 本守护叫页面把缓冲的片段传回本机 →
  合成 mp4 → 交给 smooth_capture --from-clip 审片投递。

用法:
    nest_warm.py "Garage camera" [--keep 6] [--chunk 10] [--stale 300]
触发:
    listener 在事件时写 <state>/warm-<slug>.trigger（内容随意）
输出:
    <state>/warm-<slug>.log，成片落 <state>/clips/
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time

HOME = pathlib.Path.home()
OPENCLAW = os.environ.get("OPENCLAW_BIN", "openclaw")
HERE = pathlib.Path(__file__).resolve().parent
SMOOTH_PY = HERE / "smooth_capture.py"
STATE = HOME / ".local" / "state" / "nest-events"
CLIPS = STATE / "clips"
WORKROOT = pathlib.Path("/tmp/nestwarm")
GH_BASE = os.environ.get("NEST_GH_BASE", "https://home.google.com/u/0/home/<your-home-id>")

JS = r"""
const offerSdp = __OFFER__;
const KEEP = __KEEP__, CHUNKMS = __CHUNKMS__;
const VER = 3;
window.__pr = window.__pr || {chunks: [], mr: null, started: false, v: 0, head: null};
const R = window.__pr;
const liveEl = () => Array.from(document.querySelectorAll('video'))
  .filter(x => !x.paused && x.videoWidth > 0 && x.readyState >= 2 && (x.offsetWidth || x.offsetHeight))
  .sort((a, b) => (b.videoWidth * b.videoHeight) - (a.videoWidth * a.videoHeight))[0];
if (!R.started || R.v !== VER) {
  try { if (R.mr && R.mr.state !== 'inactive') R.mr.stop(); } catch (e) {}
  R.chunks = []; R.head = null;
  const v = liveEl();
  if (!v || !v.captureStream) { return {sdp: null, reason: 'no-live-video'}; }
  R.stream = v.captureStream();
  const types = ['video/mp4;codecs=avc1.42E01E', 'video/mp4', 'video/webm;codecs=vp8', 'video/webm'];
  const mt = types.find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || '';
  R.mr = new MediaRecorder(R.stream, mt ? {mimeType: mt, videoBitsPerSecond: 1000000} : undefined);
  R.mr.ondataavailable = e => {
    if (!e.data || !e.data.size) return;
    if (!R.head) R.head = e.data;   // 第一段含容器头，必须永久保留，否则拼出来的文件无法播放
    R.chunks.push(e.data);
    while (R.chunks.length > KEEP) R.chunks.shift();
  };
  R.mr.start(CHUNKMS);
  R.started = true; R.v = VER;
}
const pc = new RTCPeerConnection();
window.__pc = pc;
pc.ondatachannel = (ev) => {
  const ch = ev.channel;
  ch.onmessage = async (m) => {
    if (String(m.data) !== 'SEND') return;
    try {
      const parts = R.head ? [R.head].concat(R.chunks.filter(c => c !== R.head)) : R.chunks.slice();
      ch.send('SIZE ' + parts.length);
      const blob = new Blob(parts, {type: 'video/mp4'});
      const buf = new Uint8Array(await blob.arrayBuffer());
      const CH = 32 * 1024;
      for (let o = 0; o < buf.length; o += CH) {
        let g = 0;
        while (ch.bufferedAmount > 2 * 1024 * 1024 && g++ < 900) await new Promise(r => setTimeout(r, 50));
        ch.send(buf.slice(o, o + CH));
      }
      ch.send('DONE ' + buf.length);
    } catch (e) {
      try { ch.send('ERR ' + String(e).slice(0, 140)); ch.send('DONE 0'); } catch (_) {}
    }
  };
};
await pc.setRemoteDescription({type: 'offer', sdp: offerSdp});
const ans = await pc.createAnswer();
await pc.setLocalDescription(ans);
for (let i = 0; i < 60 && pc.iceGatheringState !== 'complete'; i++) await new Promise(r => setTimeout(r, 200));
return {sdp: pc.localDescription.sdp, ice: pc.iceGatheringState,
        buffered: R.chunks.length, started: R.started, src: R.stream ? 'captureStream' : 'none'};
"""


SEEN = {"m": 0.0}   # 已处理的触发时间（跨会话保留：不漏启动前刚到的、也不重复取）


def run(cmd, timeout=180):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("camera")
    ap.add_argument("--keep", type=int, default=6)
    ap.add_argument("--chunk", type=int, default=10)
    ap.add_argument("--stale", type=int, default=300, help="触发文件多久算过期（秒）")
    a = ap.parse_args()

    s = slug(a.camera)
    work = WORKROOT / s
    work.mkdir(parents=True, exist_ok=True)
    trigger = STATE / f"warm-{s}.trigger"
    blob = work / "ring.bin"
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"warm-{s}.pid").write_text(str(os.getpid()), encoding="utf-8")
    print(f"[warm] pid={os.getpid()} → warm-{s}.pid", flush=True)
    label = f"nestwarm{s[:10]}"

    cmap = json.loads((HERE / "gh-cameras.json").read_text()) if (HERE / "gh-cameras.json").exists() else {}
    seg = cmap.get(a.camera) or cmap.get(a.camera.lower()) or ""
    url = f"{GH_BASE}/cameras/list/{seg}" if seg else f"{GH_BASE}/cameras/grid"

    print(f"[warm] {a.camera} · keep={a.keep}×{a.chunk}s · url=.../{seg[-12:]}", flush=True)
    # 复用已有标签页：绝不再开新页（否则 Chrome 里会堆出一堆 OpenClaw 标签组）
    have = False
    try:
        rc, out, err = run([OPENCLAW, "browser", "--browser-profile", "chrome", "tabs"], timeout=60)
        have = label in (out or "")
    except Exception:
        have = False
    if have:
        print("[warm] 复用已有标签页（不新建）", flush=True)
    else:
        try:
            run([OPENCLAW, "browser", "--browser-profile", "chrome", "open", url, "--label", label], timeout=150)
        except Exception as e:
            print(f"[warm] open 调用异常（{type(e).__name__}），稍后重试", flush=True)
    time.sleep(8)

    import asyncio
    from aiortc import RTCPeerConnection, RTCSessionDescription

    while True:
        try:
            asyncio.run(session(a, work, label, trigger, blob, JS, RTCPeerConnection, RTCSessionDescription, run))
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"[warm] session error: {type(e).__name__}: {str(e)[:160]}", flush=True)
        time.sleep(10)


async def session(a, work, label, trigger, blob, js_tpl, RTCPeerConnection, RTCSessionDescription, run):
    import asyncio

    pc = RTCPeerConnection()
    ch = pc.createDataChannel("ring")
    st = {"bytes": 0, "sending": False, "done": False, "note": "", "chunks": 0}

    @ch.on("message")
    def on_msg(msg):
        on_msg_impl(msg)

    def on_msg_impl(msg):
        if isinstance(msg, str):
            print(f"[warm] page: {msg[:120]}", flush=True)
            st["note"] = msg
            if msg.startswith("SIZE"):
                try:
                    st["chunks"] = int(msg.split()[1])
                except Exception:
                    pass
            if msg.startswith("DONE"):
                st["done"] = True
            return
        st["bytes"] += len(msg)
        with open(blob, "ab") as f:
            f.write(msg)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    for _ in range(100):
        if pc.iceGatheringState == "complete":
            break
        await asyncio.sleep(0.2)
    js = js_tpl.replace("__OFFER__", json.dumps(pc.localDescription.sdp)) \
               .replace("__KEEP__", str(a.keep)).replace("__CHUNKMS__", str(a.chunk * 1000))
    rc, out, err = run([OPENCLAW, "browser", "--browser-profile", "chrome", "evaluate",
                        "--fn", js, "--target-id", label, "--timeout-ms", "120000"], timeout=300)
    try:
        data = json.loads(out)
    except Exception:
        data = {}
    sdp = data.get("sdp")
    print(f"[warm] inject rc={rc} started={data.get('started')} buffered={data.get('buffered')} "
          f"reason={data.get('reason','')}", flush=True)
    if not sdp:
        await pc.close()
        await asyncio.sleep(5)
        return
    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))
    for _ in range(60):
        if pc.connectionState == "connected":
            break
        await asyncio.sleep(0.5)
    print(f"[warm] conn={pc.connectionState}", flush=True)

    # 触发循环；ready 健康标记：只有连着的时候才写，守听据此决定走预录还是回退旧流程
    ready = STATE / f"warm-{slug(a.camera)}.ready"
    while pc.connectionState in ("new", "connecting", "connected"):
        await asyncio.sleep(1)
        try:
            ready.write_text(str(int(time.time())), encoding="utf-8")
        except OSError:
            pass
        if not trigger.exists():
            continue
        m = trigger.stat().st_mtime
        if m <= SEEN["m"]:
            continue
        SEEN["m"] = m
        if time.time() - m > a.stale:
            print(f"[warm] 触发过旧（{int(time.time() - m)}s），忽略", flush=True)
            continue
        print("[warm] TRIGGER → 取缓冲", flush=True)
        try:
            blob.unlink()
        except OSError:
            pass
        st.update({"bytes": 0, "done": False})
        ch.send("SEND")
        t0 = time.time()
        while not st["done"] and time.time() - t0 < 90:
            await asyncio.sleep(0.5)
        size = blob.stat().st_size if blob.exists() else 0
        print(f"[warm] got {size // 1024} KB · note={st['note'][:40]}", flush=True)
        if size < 20_000:
            print("[warm] 缓冲过小，跳过", flush=True)
            continue
        out_mp4 = CLIPS / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.camera.replace(' ', '_')}-preroll.mp4"
        CLIPS.mkdir(parents=True, exist_ok=True)
        out_mp4.write_bytes(blob.read_bytes())
        # 缓冲容器自检：缺头段 → 拼出来无法播放（10:00 那次就是这么丢的）
        ok = False
        try:
            import av as _av
            _c = _av.open(str(out_mp4))
            ok = any(s.type == "video" for s in _c.streams) and next(iter(_c.decode(video=0)), None) is not None
            _c.close()
        except Exception as e:
            print(f"[warm] 缓冲片无效: {type(e).__name__}: {str(e)[:110]}", flush=True)
        if not ok:
            print("[warm] 缓冲片不完整（很可能缺容器头），跳过本次并重置录制", flush=True)
            continue
        # 交给既有审片+投递链路（复用三态逻辑）
        rc, out2, err2 = run([sys.executable, str(SMOOTH_PY), a.camera,
                              "--from-clip", str(out_mp4), "--judge", "--auto-send", "--send", "--person",
                              "--real-seconds", str((st.get("chunks") or 0) * a.chunk),
                              "--caption", f"🎥 {a.camera} · 事件预录（{a.keep * a.chunk} 秒环缓冲）"],
                             timeout=600)
        print(f"[warm] 审片投递 rc={rc} {(out2 or err2)[-160:]}", flush=True)
    await pc.close()
    try:
        ready.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
