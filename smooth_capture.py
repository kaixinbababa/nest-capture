#!/usr/bin/env python3
"""流畅片段：页面内 MediaRecorder 录原生流 → WebRTC 数据通道回传 → mp4 → 推送。

为什么绕这么大圈：Google 的 SDM API 不给视频（WebRTC 视频轨收不到帧、事件静态图被拒），
页面内的数据又出不来（CSP 拦 fetch/XHR，混合内容拦表单，Chrome 的"下载前询问"拦存盘）。
WebRTC 数据通道不受这些限制，是唯一能拿到**连续画面**的通道。

关键点：录**视频元素的原生流**（v.captureStream()），不做逐帧 JS 编码 ——
逐帧 canvas+toBlob 会被 Chrome 后台限流到 1-2fps，而 MediaRecorder 走的是原生编码器，
帧率就是摄像头原始帧率（实测 ~15-30fps，真流畅）。

用法:
    ~/.venvs/nest/bin/python smooth_capture.py "DOORBELL" [--seconds 20] [--send] [--caption "..."]
    ~/.venvs/nest/bin/python smooth_capture.py "DOORBELL" --tab <已有标签> --seconds 20 --send
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
WORK = pathlib.Path("/tmp/nestdc")
BLOB = WORK / "clip.bin"
STATE = HOME / ".local" / "state" / "nest-events"
CLIPS = STATE / "clips"
MEDIA_DIR = HOME / ".openclaw" / "media" / "outbound"
REVIEW_DIR = HOME / ".openclaw" / "workspace" / "tmp" / "nest-review"
JUDGE_LOG = STATE / "judge.log"
JUDGE_MODEL = "openai/gpt-5.4-mini"
JUDGE_PROMPT = (
    "这是同一个监控事件按时间顺序抽取的几帧画面（门铃/车库摄像头）。请判断要不要打扰屋主：\n"
    "1) 画面里有人吗？几个人？有没有车、动物？\n"
    "2) 如果有人：他是朝房子/门口靠近（在画面里逐渐变大、走向镜头），还是只是横穿画面路过？\n"
    "3) 这是'值得提醒屋主的安全事件'还是'无关噪音'（车流、路人、树影、宠物、自己家人出门）？\n"
    "请严格按下面三行输出，不要多余内容：\n"
    "SUMMARY: 一句话标题（≤20字），写清「谁 / 在做什么 / 朝哪」（例：1名快递员走向门口；街边行人路过）\n"
    "REASON: 一句话理由\n"
    "JUDGE: NOTIFY 或 JUDGE: NOISE"
)
HERE = pathlib.Path(__file__).resolve().parent
RECEIVER = HERE / "webdc_receiver.py"
MAPFILE = HERE / "gh-cameras.json"
GH_BASE = os.environ.get("NEST_GH_BASE", "https://home.google.com/u/0/home/<your-home-id>")
TARGET = os.environ.get("NEST_TG_TARGET", "<your-telegram-chat-id>")
THREAD_ID = os.environ.get("NEST_TG_THREAD", "<your-thread-id>")

JS_TEMPLATE = r"""
const offerSdp = __OFFER__;
const SECONDS = __SECONDS__;
const pc = new RTCPeerConnection();
window.__nestpc = pc;
window.__nestlog = [];
pc.onconnectionstatechange = () => window.__nestlog.push('pc:' + pc.connectionState);
pc.ondatachannel = (ev) => {
  const ch = ev.channel;
  window.__nestch = ch;
  const say = (s) => { try { ch.send(String(s)); } catch (e) {} };
  ch.onopen = async () => {
    window.__nestlog.push('open');
    say('OPEN');
    try {
      // 关键：页面里有多个 video（含暂停的旧缓冲/缩略图），必须挑“正在播放且可见”的那个
      const cands = Array.from(document.querySelectorAll('video')).filter(x =>
        !x.paused && x.videoWidth > 0 && x.readyState >= 2 && (x.offsetWidth || x.offsetHeight));
      cands.sort((a, b) => (b.videoWidth * b.videoHeight) - (a.videoWidth * a.videoHeight));
      const v = cands[0];
      if (!v || !v.captureStream) {
        const all = Array.from(document.querySelectorAll('video')).map(x => x.videoWidth + 'x' + x.videoHeight + (x.paused ? '/paused' : '/play'));
        say('NOLIVEVIDEO ' + all.join(',')); say('DONE'); return;
      }
      // 再确认画面真的在动（防止抓成冻结帧）
      const q = () => (v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality().totalVideoFrames : -1);
      const f0 = q(), t0 = v.currentTime;
      await new Promise(r => setTimeout(r, 1500));
      const f1 = q(), t1 = v.currentTime;
      say('LIVE frames ' + f0 + '->' + f1 + ' t=' + t0.toFixed(2) + '->' + t1.toFixed(2));
      if (f1 <= f0 && Math.abs(t1 - t0) < 0.2) { say('FROZEN'); say('DONE'); return; }
      say('VIDEO ' + v.videoWidth + 'x' + v.videoHeight);
      const stream = v.captureStream();
      const types = ['video/mp4;codecs=avc1.42E01E', 'video/mp4', 'video/webm;codecs=vp8', 'video/webm'];
      const mt = types.find(t => window.MediaRecorder && MediaRecorder.isTypeSupported(t)) || '';
      const mr = new MediaRecorder(stream, mt ? {mimeType: mt, videoBitsPerSecond: 2500000} : undefined);
      const parts = [];
      mr.ondataavailable = e => { if (e.data && e.data.size) parts.push(e.data); };
      mr.onstop = async () => {
        try {
          const blob = new Blob(parts, {type: mr.mimeType || 'video/webm'});
          say('BLOB ' + blob.size + ' ' + (mr.mimeType || ''));
          const buf = new Uint8Array(await blob.arrayBuffer());
          const CHUNK = 32 * 1024;
          for (let o = 0; o < buf.length; o += CHUNK) {
            let g = 0;
            while (ch.bufferedAmount > 2 * 1024 * 1024 && g++ < 600) await new Promise(r => setTimeout(r, 50));
            ch.send(buf.slice(o, o + CHUNK));
          }
          say('DONE');
        } catch (e) { say('ERR ' + String(e).slice(0, 140)); say('DONE'); }
      };
      mr.start(1000);
      setTimeout(() => { try { mr.stop(); } catch (e) {} }, SECONDS * 1000);
    } catch (e) { say('ERR ' + String(e).slice(0, 140)); say('DONE'); }
  };
};
await pc.setRemoteDescription({type: 'offer', sdp: offerSdp});
const ans = await pc.createAnswer();
await pc.setLocalDescription(ans);
for (let i = 0; i < 60 && pc.iceGatheringState !== 'complete'; i++) await new Promise(r => setTimeout(r, 200));
return {sdp: pc.localDescription.sdp, ice: pc.iceGatheringState, log: window.__nestlog};
"""


def run(cmd, timeout=240):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def save_jpeg(frame, path):
    """把一帧存成 JPEG（纯 PyAV，不依赖 PIL）。"""
    import av
    with av.open(str(path), "w") as out:
        st = out.add_stream("mjpeg", rate=1)
        st.width, st.height = frame.width - (frame.width % 2), frame.height - (frame.height % 2)
        st.pix_fmt = "yuvj420p"
        f = frame.reformat(width=st.width, height=st.height, format="yuvj420p")
        f.pts = 0
        for pkt in st.encode(f):
            out.mux(pkt)
    return path


def sample_frames(mp4, outdir, n=4):
    """从成片里均匀抽 n 帧交给视觉模型判断。"""
    import av
    outdir.mkdir(parents=True, exist_ok=True)
    frames = []
    c = av.open(str(mp4))
    for fr in c.decode(video=0):
        frames.append(fr)
    c.close()
    if not frames:
        return []
    paths = []
    for i in range(n):
        idx = min(int(len(frames) * i / max(n - 1, 1)), len(frames) - 1)
        paths.append(save_jpeg(frames[idx], outdir / f"j{i + 1}.jpg"))
    return paths


def judge(frames):
    """交给视觉模型评估，返回 (verdict, 完整回答)。"""
    cmd = [OPENCLAW, "infer", "model", "run", "--gateway", "--model", JUDGE_MODEL]
    for p in frames:
        cmd += ["--file", str(p)]
    cmd += ["--prompt", JUDGE_PROMPT]
    rc, out, err = run(cmd, timeout=300)
    text = out or err
    if "JUDGE: NOTIFY" in text:
        return "NOTIFY", text
    if "JUDGE: NOISE" in text:
        return "NOISE", text
    return "UNCLEAR", text


SEND_MAX_MB = 4.5


def normalize_for_send(src, max_mb=SEND_MAX_MB, real_dur=0.0):
    """把浏览器直出的碎片 MP4 规范化：恒定帧率 + faststart + 规整关键帧。
    目的：@Telegram 里能顺畅拖动进度条快进快退（碎片 MP4 的时间戳是乱的）。
    帧率按实际（帧数 / 真实时长）推算，避免变成慢动作。"""
    import av
    from fractions import Fraction
    try:
        inp = av.open(str(src))
        vs = inp.streams.video[0]
        frames = [f for f in inp.decode(video=0)]
        dur = float(vs.duration * vs.time_base) if vs.duration else 0.0
        src_h, src_w = vs.height, vs.width
        inp.close()
        if not frames:
            return src
        fps = max(10, min(30, round(len(frames) / (real_dur or dur)))) if (real_dur or dur) > 0.5 else 15
        h = 540 if src_h > 600 else src_h
        w = int(h * (src_w / max(src_h, 1)))
        w -= w % 2
        h -= h % 2
        dst = src.with_name(f"{src.stem}-norm.mp4")
        out = av.open(str(dst), "w", options={"movflags": "+faststart"})
        st = out.add_stream("libx264", rate=Fraction(fps, 1))
        st.width, st.height, st.pix_fmt = w, h, "yuv420p"
        st.options = {"crf": "30", "preset": "veryfast", "g": str(fps * 2)}
        for i, f in enumerate(frames):
            fr = f.reformat(width=w, height=h)
            fr.pts = i
            fr.time_base = Fraction(1, fps)
            for pkt in st.encode(fr):
                out.mux(pkt)
        for pkt in st.encode():
            out.mux(pkt)
        out.close()
        sz = dst.stat().st_size / 1024 / 1024
        ok = False
        try:
            c = av.open(str(dst))
            ok = next(iter(c.decode(video=0)), None) is not None
            c.close()
        except Exception:
            ok = False
        print(f"[smooth] 规范化: {fps}fps {w}x{h} · {len(frames)} 帧 · {sz:.2f} MB · "
              f"真实时长 {real_dur or dur:.0f}s · faststart · {'可用' if ok else '不可用'}", flush=True)
        if ok and 0.01 < sz <= max_mb:
            return dst
    except Exception as e:
        print(f"[smooth] 规范化失败（回退）: {type(e).__name__}: {str(e)[:90]}", flush=True)
    return src


def locate_person(mp4, n=8, real_dur=0.0):
    """在整段片子里定位“人”出现在第几秒（一次视觉调用）。返回 [(起, 止), ...]。
    real_dur：片子的真实时长（碎片 MP4 的容器时长虚高，不用它会算出荒唐坐标）。"""
    import av
    import re as _re
    review = REVIEW_DIR / f"locate-{time.strftime('%Y%m%d-%H%M%S')}"
    shots = sample_frames(mp4, review, n)
    if not shots:
        return []
    c = av.open(str(mp4))
    v = c.streams.video[0]
    dur = float(v.duration * v.time_base) if v.duration else 0.0
    c.close()
    if real_dur and real_dur > 1:
        dur = real_dur
    if dur < 1 or dur > 600:
        dur = 30.0
    args = []
    for p in shots:
        args += ["--file", str(p)]
    q = (f"这是同一段监控视频按时间顺序抽取的 {len(shots)} 帧，整段约 {dur:.0f} 秒。"
         "请严格判断：哪些帧里能看到人（哪怕远处的、很小的人影）？"
         "宁可漏报也不要误报：看不清、只是树影/车/光影变化的一律不算。"
         "都没有人就回答：NONE。有的话只回答帧号（从 1 开始，逗号分隔），例如：3,4。不要解释。")
    rc, out, err = run([OPENCLAW, "infer", "model", "run", "--gateway", "--model", JUDGE_MODEL]
                       + args + ["--prompt", q], timeout=300)
    if "NONE" in (out or "").upper():
        print("[smooth] 人影定位: 无", flush=True)
        return []
    idx = sorted({int(x) for x in _re.findall(r"\d+", out or "") if 1 <= int(x) <= len(shots)})
    hits = []
    for i in idx:
        t = (i - 1) * dur / max(1, len(shots) - 1)
        hits.append((max(0.0, t - 2), min(dur, t + 2)))
    print(f"[smooth] 人影定位: 帧 {idx} / {len(shots)} → "
          f"{[f'{int(s)}-{int(e)}s' for s, e in hits] or '无'}", flush=True)
    return hits


def shrink_for_send(src, max_mb=SEND_MAX_MB):
    """投递上限 5MB：超限就按“保留最后 N 秒”重剪（流拷贝，不重编码 → 快、不会产出空文件）。"""
    import av
    if src.stat().st_size / 1024 / 1024 <= max_mb:
        return src
    for secs in (30, 20, 12, 6):
        dst = src.with_name(f"{src.stem}-cut{secs}.mp4")
        try:
            inp = av.open(str(src))
            out = av.open(str(dst), "w")
            ist = inp.streams.video[0]
            ost = out.add_stream(template=ist)
            dur = float(ist.duration * ist.time_base) if ist.duration else 0.0
            base = max(0.0, dur - secs)
            n = 0
            for pkt in inp.demux(ist):
                if pkt.pts is None or pkt.dts is None:
                    continue
                t = float(pkt.pts * pkt.time_base)
                if t < base:
                    continue
                pkt.stream = ost
                out.mux(pkt)
                n += 1
            out.close()
            inp.close()
            sz = dst.stat().st_size / 1024 / 1024
            ok = False
            if sz > 0.01:
                try:
                    c = av.open(str(dst))
                    ok = next(iter(c.decode(video=0)), None) is not None
                    c.close()
                except Exception:
                    ok = False
            print(f"[smooth] 重剪 {secs}s → {sz:.1f} MB · {n} 包 · {'可用' if ok else '不可用'}", flush=True)
            if ok and sz <= max_mb:
                return dst
        except Exception as e:
            print(f"[smooth] 重剪 {secs}s 失败: {type(e).__name__}: {str(e)[:80]}", flush=True)
    return src


def convert(src_bin, out_mp4):
    """把页面录到的容器（webm/mp4）转成 H.264 mp4（Telegram 播放最稳）。"""
    import av
    from fractions import Fraction
    inp = av.open(str(src_bin))
    vs = inp.streams.video[0]
    rate = vs.average_rate if vs.average_rate and float(vs.average_rate) > 1 else Fraction(15, 1)
    out = av.open(str(out_mp4), "w")
    ost = out.add_stream("libx264", rate=rate)
    ost.pix_fmt = "yuv420p"
    ost.bit_rate = 3_000_000
    w = h = None
    n = 0
    for frame in inp.decode(video=0):
        if w is None:
            w = frame.width - (frame.width % 2)
            h = frame.height - (frame.height % 2)
            ost.width, ost.height = w, h
        nf = frame.reformat(width=w, height=h, format="yuv420p")
        nf.pts = None
        for pkt in ost.encode(nf):
            out.mux(pkt)
        n += 1
    for pkt in ost.encode():
        out.mux(pkt)
    inp.close()
    out.close()
    return n, rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("camera")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--caption", default="")
    ap.add_argument("--tab", default="", help="复用已有标签页（否则新开）")
    ap.add_argument("--judge", action="store_true", help="用视觉模型审片后再决定")
    ap.add_argument("--auto-send", action="store_true", help="仅当判定为安全事件时推送")
    ap.add_argument("--test-verdict", default="", help="自测用：强制 verdict（NOTIFY/NOISE/UNCLEAR）")
    ap.add_argument("--from-clip", default="", help="跳过抓拍：直接对已有成片做审片+投递（预录守护用）")
    ap.add_argument("--person", action="store_true", help="本次是「看到人」事件：审片判 NOTIFY 时额外定位人影出现的秒数（NOISE 一律静默，不再推送）")
    ap.add_argument("--real-seconds", type=float, default=0.0,
                    help="片子的真实时长（秒）。预录守护按“片段数 × 每段秒数”给出，用于算对帧率（碎片 MP4 的时长元数据虚高，不修正会变慢动作）")
    a = ap.parse_args()

    if a.from_clip:
        mp4 = pathlib.Path(a.from_clip)
        if not mp4.exists():
            print(f"[smooth] from-clip 不存在: {mp4}", flush=True)
            return 1
        print(f"[smooth] from-clip → {mp4.name}", flush=True)
        return judge_and_send(mp4, a)

    cmap = json.loads(MAPFILE.read_text()) if MAPFILE.exists() else {}
    seg = (cmap.get(a.camera) or cmap.get(a.camera.lower()) or cmap.get("_default") or "")
    url = f"{GH_BASE}/cameras/list/{seg}" if seg else f"{GH_BASE}/cameras/grid"

    CLIPS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    # 固定标签页：每台相机最多一个（避免每次抓拍都新建 → Chrome 里堆满 OpenClaw 标签组）
    label = a.tab or f"nestwarm{a.camera.replace(' ', '_')[:10]}"
    if not a.tab:
        rc, out, err = run([OPENCLAW, "browser", "--browser-profile", "chrome", "tabs"], timeout=60)
        if label not in (out or ""):
            rc, out, err = run([OPENCLAW, "browser", "--browser-profile", "chrome",
                                "open", url, "--label", label], timeout=120)
            if rc != 0:
                print(f"[smooth] open failed rc={rc} {err[:200]}", flush=True)
                return 1
            time.sleep(7)  # 等实时画面
        else:
            print(f"[smooth] 复用已有标签页 {label}（不新建）", flush=True)
    # 抓帧期间把标签页置前（后台标签页会被限流）
    run([OPENCLAW, "browser", "--browser-profile", "chrome", "focus", label], timeout=60)
    time.sleep(2)

    # 起接收端
    logf = open(WORK / "recv.log", "w")
    for f in ("offer.sdp", "answer.sdp", "ready", "clip.bin"):
        try:
            (WORK / f).unlink()
        except OSError:
            pass
    recv = subprocess.Popen([sys.executable, "-u", str(RECEIVER), "--timeout", str(int(a.seconds) + 120)],
                            stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):
        if (WORK / "offer.sdp").exists():
            break
        time.sleep(0.5)
    if not (WORK / "offer.sdp").exists():
        print("[smooth] no offer", flush=True)
        recv.terminate()
        return 1
    offer = (WORK / "offer.sdp").read_text()

    js = JS_TEMPLATE.replace("__OFFER__", json.dumps(offer)).replace("__SECONDS__", str(a.seconds))
    rc, out, err = run([OPENCLAW, "browser", "--browser-profile", "chrome", "evaluate",
                        "--fn", js, "--target-id", label, "--timeout-ms", "120000"], timeout=300)
    try:
        data = json.loads(out)
        sdp = data.get("sdp")
    except Exception:
        sdp = None
        print(f"[smooth] inject failed: {out[:200]} | {err[:200]}", flush=True)
    if not sdp:
        recv.terminate()
        return 1
    (WORK / "answer.sdp").write_text(sdp)
    print("[smooth] handshake ok, recording...", flush=True)

    deadline = time.time() + a.seconds + 120
    while time.time() < deadline:
        time.sleep(2)
        if "RESULT" in (WORK / "recv.log").read_text():
            break
        if recv.poll() is not None:
            break
    recv.terminate()

    size = BLOB.stat().st_size if BLOB.exists() else 0
    print(f"[smooth] blob {size // 1024} KB", flush=True)
    # 不再关闭标签页：留着复用（每台相机固定一个），避免反复新建标签组
    if size < 20_000:
        print("[smooth] blob too small", flush=True)
        return 1

    # 先校验页面传回的容器是否完整 —— 截断的 blob 会转出“半成品 mp4”，
    # 之前 08:24 那次就是这样静默丢片的（有文件、但无法解析、也没走到审片）
    import av as _av
    try:
        _p = _av.open(str(BLOB))
        _has_video = any(s.type == "video" for s in _p.streams)
        _p.close()
        if not _has_video:
            print("[smooth] blob 里没有视频流，放弃", flush=True)
            return 1
    except Exception as e:
        print(f"[smooth] blob 无法解析（传输可能被截断）: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return 1

    mp4 = CLIPS / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.camera.replace(' ', '_')}-smooth.mp4"
    try:
        frames, rate = convert(BLOB, mp4)
    except Exception as e:
        print(f"[smooth] 转码失败: {type(e).__name__}: {str(e)[:140]}", flush=True)
        try:
            mp4.unlink()
        except OSError:
            pass
        return 1
    # 成片自检：必须能解出帧，否则丢弃（不推废片）
    try:
        _c = _av.open(str(mp4))
        _first = next(iter(_c.decode(video=0)), None)
        _declared = _c.streams.video[0].frames
        _c.close()
    except Exception:
        _first, _declared = None, 0
    if _first is None or not _declared:
        print("[smooth] 成片无效（0 帧），丢弃", flush=True)
        try:
            mp4.unlink()
        except OSError:
            pass
        return 1
    print(f"[smooth] wrote {mp4.name} · {frames} frames · {rate} fps · {mp4.stat().st_size // 1024} KB", flush=True)

    return judge_and_send(mp4, a)


def _stamp():
    """事件时间戳（本地时区），让屋主一眼看到事件发生的**明确时刻**。"""
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def _title_from(answer):
    """从审片回答里抽出 SUMMARY 标题（视频旁边那句"一句话总结"）。"""
    import re as _re
    m = _re.search(r"SUMMARY\s*[:：]\s*(.+)", answer or "")
    if not m:
        return ""
    return " ".join(m.group(1).split()).strip(" 。.、")[:40]


def judge_and_send(mp4, a):
    """审片 + 按三态投递（NOTIFY 推视频 / UNCLEAR 也推视频 / NOISE 静默）。
    既用于正常抓拍，也用于 --from-clip（预录守护把片送进来时）。"""
    # —— 审片：让视觉模型判断要不要打扰屋主 ——
    verdict = "SKIP"
    answer = ""
    if a.judge:
        review = REVIEW_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{a.camera.replace(' ', '_')}"
        shots = sample_frames(mp4, review, 4)
        if shots:
            verdict, answer = judge(shots)
        else:
            verdict, answer = "UNCLEAR", "(no frames)"
        clean = " ".join((answer or "").split())[:400]
        try:
            JUDGE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with JUDGE_LOG.open("a") as f:
                f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "camera": a.camera,
                                    "clip": mp4.name, "verdict": verdict, "answer": clean},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass
        print(f"[judge] {verdict} · {clean[:200]}", flush=True)
    if a.test_verdict:
        verdict = a.test_verdict.upper()
        print(f"[judge] (test override) verdict={verdict}", flush=True)

    # 三态投递：NOTIFY 推视频；UNCLEAR（判不准）也推视频交给屋主自己看；NOISE 一律静默
    if a.judge and a.auto_send:
        if verdict == "NOTIFY":
            mode = "clip"
        elif verdict == "UNCLEAR":
            mode = "clip"
        else:
            mode = "none"
    elif a.send:
        mode = "clip"
    else:
        mode = "none"

    if mode == "none":
        print(f"[smooth] verdict={verdict} → 不推送（静默）", flush=True)
        return 0

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if mode == "clip":
        # ① 定位人影：只在审片确实看到人时才标（否则坐标会误导），且用真实时长换算
        hits = []
        if getattr(a, "person", False) and verdict == "NOTIFY":
            try:
                hits = locate_person(mp4, 8, real_dur=getattr(a, "real_seconds", 0.0) or 0.0)
            except Exception as e:
                print(f"[smooth] 定位人影失败: {type(e).__name__}: {str(e)[:80]}", flush=True)
        # ② 规范化：恒定帧率 + faststart → Telegram 里可顺畅拖动快进快退
        try:
            sendable = normalize_for_send(mp4, real_dur=getattr(a, "real_seconds", 0.0) or 0.0)
        except Exception as e:
            print(f"[smooth] 规范化失败，改用原片: {type(e).__name__}: {str(e)[:80]}", flush=True)
            sendable = mp4
        if sendable.stat().st_size / 1024 / 1024 > SEND_MAX_MB:
            try:
                sendable = shrink_for_send(sendable)
            except Exception:
                pass
        sp = MEDIA_DIR / sendable.name
        shutil.copy(sendable, sp)
        title = _title_from(answer)
        caption = f"🕒 {_stamp()}\n"
        if title:
            caption += f"🏷️ {title}\n"
        if verdict == "UNCLEAR":
            caption += "⚠️ 审片没判准（模型/网络异常）—— 这段我拿不准，你自己看一眼\n"
        caption += (a.caption or f"🎥 {a.camera} · {a.seconds:.0f}s")
        if hits:
            caption += ("\n👤 画面里有人出现在：" +
                        "、".join(f"第 {int(s)} 秒" for s, _ in hits) +
                        "（进度条可前后拖动看）")
        elif getattr(a, "person", False) and verdict != "UNCLEAR":
            caption += "\n⚠️ 这段里我没找到清晰人影（可能是街上路人/远处）—— 拖进度条自己看看"
        rc, out, err = run([OPENCLAW, "message", "send", "--channel", "telegram",
                            "--target", TARGET, "--thread-id", THREAD_ID,
                            "--message", caption, "--media", str(sp),
                            "--force-document"], timeout=300)
    else:
        caption = ((f"🕒 {_stamp()}\n" + (a.caption or "")) +
                   "\n⚠️ 门口有活动，但审片没判准（模型/网络异常），这条不带视频，麻烦你自己瞄一眼。")
        rc, out, err = run([OPENCLAW, "message", "send", "--channel", "telegram",
                            "--target", TARGET, "--thread-id", THREAD_ID,
                            "--message", caption], timeout=300)
    print(f"[smooth] send({mode}) rc={rc} {out[:100] or err[:140]}", flush=True)
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
