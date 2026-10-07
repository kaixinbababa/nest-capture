#!/usr/bin/env python3
"""Google Nest SDM 一次性 OAuth（loopback 回调）。

用法:
    ~/.venvs/nest/bin/python sdm_oauth.py --port 8085

前置: ~/.config/nest-sdm/client.json
    {"client_id": "...", "client_secret": "...", "project_id": "<Device Access Project ID>"}

成功后在 ~/.config/nest-sdm/token.json 写入 refresh token（权限 600）。
redirect_uri 必须与 OAuth 客户端里登记的一致: http://127.0.0.1:8085/
"""
import argparse
import http.server
import json
import pathlib
import socketserver
import urllib.parse
import webbrowser

import requests

CONF = pathlib.Path.home() / ".config" / "nest-sdm"
# SDM 事件需要 pubsub 权限（本地用 REST 建 topic/订阅/拉消息）
SCOPE = " ".join([
    "https://www.googleapis.com/auth/sdm.service",
    "https://www.googleapis.com/auth/pubsub",
])
AUTH = "https://nestservices.google.com/partnerconnections/{pid}/auth"
TOKEN = "https://oauth2.googleapis.com/token"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8085)
    a = ap.parse_args()

    cred = json.loads((CONF / "client.json").read_text())
    redirect = f"http://127.0.0.1:{a.port}/"
    url = AUTH.format(pid=cred["project_id"]) + "?" + urllib.parse.urlencode({
        "redirect_uri": redirect,
        "access_type": "offline",
        "prompt": "consent",
        "client_id": cred["client_id"],
        "response_type": "code",
        "scope": SCOPE,
    })
    print("\n打开下面这个链接，用管摄像头的 Google 账号登录并点【允许】:\n\n" + url + "\n")

    box = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            p = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in p:
                box["code"] = p["code"][0]
                body = "<h2>&#10004; 授权完成，可以关闭此页面</h2>".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif "error" in p:
                box["error"] = p["error"][0]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"error")
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    with socketserver.TCPServer(("127.0.0.1", a.port), H) as srv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
        while "code" not in box and "error" not in box:
            srv.handle_request()

    if "error" in box:
        raise SystemExit(f"授权失败: {box['error']}")

    r = requests.post(TOKEN, data={
        "code": box["code"],
        "client_id": cred["client_id"],
        "client_secret": cred["client_secret"],
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    out = CONF / "token.json"
    out.write_text(json.dumps(tok, indent=2))
    out.chmod(0o600)
    print(f"token 已保存: {out}")
    print("scope:", tok.get("scope"))
    print("有 refresh_token:", bool(tok.get("refresh_token")))


if __name__ == "__main__":
    main()
