#!/usr/bin/env python3
"""
推送出口（launchd 08:00 调这个）—— 系统里唯一会真正发出通知的地方。

它也是失败看门狗：队列里今天没有 daily 条目，就说明计算任务没跑成
（可能是被 OOM killer 干掉这类 try/except 抓不到的情况），主动推一条告知。
用户不会每天开 app，「没有消息」绝不能等于「没事」。

用法：
  python3 run_notify.py              # 正常发
  python3 run_notify.py --dry-run    # 只打印不发
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sw.config import CFG
from sw.store import Store
from sw import notify as NT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    st = Store.open_read_only(CFG.db_path) if a.dry_run else Store(CFG.db_path)
    try:
        r = NT.drain(st, CFG, dry_run=a.dry_run)
        print(f"[{datetime.now().isoformat(timespec='seconds')}] "
              f"发出 {r['sent']} 条，失败 {r['failed']} 条"
              + ("，已上报计算任务失败" if r["failure_reported"] else ""))
        return 0 if r["failed"] == 0 else 1
    finally:
        st.close()


if __name__ == "__main__":
    sys.exit(main())
