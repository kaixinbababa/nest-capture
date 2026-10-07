#!/usr/bin/env python3
"""Google Nest SDM 小工具。

用法:
    ~/.venvs/nest/bin/python sdm.py devices             # 列出所有设备 + 各自 traits
    ~/.venvs/nest/bin/python sdm.py get <device_id>     # 打印单个设备完整 traits(JSON)
    ~/.venvs/nest/bin/python sdm.py cmd <device_id> <command> [json_params]

依赖: ~/.config/nest-sdm/client.json + token.json（由 sdm_oauth.py 生成）
"""
import json
import pathlib
import sys

import requests

CONF = pathlib.Path.home() / ".config" / "nest-sdm"
# 注意：REST host 是 smartdevicemanagement.googleapis.com（不是 smartdevices）
BASE = "https://smartdevicemanagement.googleapis.com/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def auth():
    cred = json.loads((CONF / "client.json").read_text())
    tok = json.loads((CONF / "token.json").read_text())
    r = requests.post(TOKEN_URL, data={
        "client_id": cred["client_id"],
        "client_secret": cred["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return cred["project_id"], r.json()["access_token"]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    pid, at = auth()
    h = {"Authorization": f"Bearer {at}"}
    cmd = sys.argv[1]

    if cmd == "devices":
        r = requests.get(f"{BASE}/enterprises/{pid}/devices", headers=h, timeout=30)
        r.raise_for_status()
        for d in r.json().get("devices", []):
            info = d.get("traits", {}).get("sdm.devices.traits.Info", {})
            did = d["name"].split("/")[-1]
            print(f"- {did} | {d['type']} | {info.get('customName', '?')}")
            print("    " + ", ".join(sorted(d.get("traits", {}).keys())))
    elif cmd == "get":
        r = requests.get(f"{BASE}/enterprises/{pid}/devices/{sys.argv[2]}", headers=h, timeout=30)
        r.raise_for_status()
        print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    elif cmd == "cmd":
        params = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {}
        r = requests.post(
            f"{BASE}/enterprises/{pid}/devices/{sys.argv[2]}:executeCommand",
            headers=h, json={"command": sys.argv[3], "params": params}, timeout=30)
        print(r.status_code, r.text[:800])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
