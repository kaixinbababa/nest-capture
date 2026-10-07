#!/usr/bin/env python3
"""把一堆截图拼成一段 mp4（不需要系统 ffmpeg，PyAV 自带 libx264）。

用法:
    ~/.venvs/nest/bin/python make_clip.py <frames-dir> <out.mp4> [--seconds 20] [--width 960]

按文件名排序取帧，均匀铺满 --seconds 秒（默认 20s）。用于"事件 → 截图序列 → 小视频"。
"""
import argparse
import pathlib
import sys
from fractions import Fraction

import av


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames_dir")
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--width", type=int, default=960)
    ap.add_argument("--fps", type=float, default=0.0, help="0 = 由 seconds 和帧数推算")
    a = ap.parse_args()

    files = sorted(p for p in pathlib.Path(a.frames_dir).glob("*.png"))
    if not files:
        print("no frames", file=sys.stderr)
        return 1
    fps = a.fps or (len(files) / a.seconds)
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    with av.open(str(out), "w") as container:
        # 强制 1 fps：显式设置 time_base + 每帧 pts，否则 PyAV 会按默认帧率写成 1 秒
        stream = container.add_stream("libx264", rate=int(round(fps)) or 1)
        stream.time_base = Fraction(1, int(round(fps)) or 1)
        stream.bit_rate = 1_200_000
        stream.pix_fmt = "yuv420p"

        first = av.open(str(files[0])).decode(video=0)
        frame0 = next(iter(first))
        w, h = frame0.width, frame0.height
        if a.width and w > a.width:
            h = int(h * a.width / w)
            w = a.width
        stream.width = w - (w % 2)
        stream.height = h - (h % 2)

        n = 0
        for f in files:
            c = av.open(str(f))
            for frame in c.decode(video=0):
                nf = frame.reformat(width=stream.width, height=stream.height, format="yuv420p")
                nf.pts = n
                nf.time_base = Fraction(1, int(round(fps)) or 1)
                for packet in stream.encode(nf):
                    container.mux(packet)
                n += 1
                break  # 每张图只取第一帧
            c.close()
        for packet in stream.encode():
            container.mux(packet)

    size = out.stat().st_size
    print(f"wrote {out} · {len(files)} frames · {fps:.2f} fps · {len(files)/fps:.1f}s · {size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
