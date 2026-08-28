#!/usr/bin/env python3
"""
ntfy 推送连通性测试。运行：python3 tools/test_ntfy.py

先在手机上装 ntfy app 并订阅 config.yaml 里的 topic，再跑这个脚本。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "stockwatch"))
import requests
from sw.config import CFG

url = CFG.ntfy_url
if not url:
    print("❌ config.yaml 的 notify.ntfy_topic 是空的")
    sys.exit(1)

print(f"topic   : {CFG.ntfy_topic}")
print(f"订阅链接 : {url}\n发送测试消息 ...")
r = requests.post(
    url,
    data="这是一条来自 StockWatch 的测试消息。手机上能看到它，推送就通了。".encode("utf-8"),
    headers={"Title": "StockWatch 连通性测试".encode("utf-8"),
             "Priority": "default", "Tags": "white_check_mark"},
    timeout=20)
print(f"{'✅ 已发送' if r.ok else '❌ 失败'}  HTTP {r.status_code}  {r.text[:200]}")
sys.exit(0 if r.ok else 1)
