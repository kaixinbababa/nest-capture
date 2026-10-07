#!/usr/bin/env python3
"""本机收件服务：给浏览器里的页面 POST 上传视频片段用。

页面在 home.google.com（https）里录制，POST 到 http://127.0.0.1:8099/upload
（Chrome 把 127.0.0.1 当可信来源，不算混合内容；这里给 CORS 头）。

用法:
    ~/.venvs/nest/bin/python capture_server.py [--port 8099]

落盘: ~/.local/state/nest-events/clips/<时间戳>-<ext>
"""
import argparse
import json
import pathlib
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLIPS = pathlib.Path.home() / ".local" / "state" / "nest-events" / "clips"
CLIPS.mkdir(parents=True, exist_ok=True)
LAST = pathlib.Path.home() / ".local" / "state" / "nest-events" / "last-upload.json"


class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        qs = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    qs[k] = v
        ext = qs.get("ext", "webm")
        tag = qs.get("tag", "clip")
        fn = CLIPS / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}.{ext}"
        fn.write_bytes(body)
        info = {"path": str(fn), "bytes": len(body), "tag": tag, "at": time.strftime("%FT%T%z")}
        LAST.write_text(json.dumps(info, ensure_ascii=False))
        print(f"[upload] {info}", flush=True)
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(info).encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args()
    print(f"capture server on http://127.0.0.1:{a.port} → {CLIPS}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
