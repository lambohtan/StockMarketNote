#!/usr/bin/env python3
"""plist 渲染测试。运行：python3 tests/test_schedule.py

只测渲染，不真的 load —— 那会改动用户的 LaunchAgents。
"""
import sys, plistlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import schedule as SC

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


class Cfg:
    def __init__(self, **kw):
        self._d = {"schedule.compute_daily": "06:00",
                   "schedule.compute_retry": "07:00",
                   "schedule.compute_weekly": "06:30",
                   "schedule.weekly_day": "tuesday",
                   "schedule.push_time": "08:00",
                   "llm.provider": "claude_cli"}
        self._d.update(kw)
    def get(self, k, d=None):
        return self._d.get(k, d)


def test_plist_is_valid_and_has_calendar():
    print("\nplist 渲染")
    xml = SC.render_plist("com.lambo.test", ["/usr/bin/python3", "x.py"],
                          {"Hour": 6, "Minute": 0}, "/tmp/wd")
    d = plistlib.loads(xml.encode("utf-8"))
    check("Label 正确", d["Label"], "com.lambo.test")
    check("时间正确", d["StartCalendarInterval"], {"Hour": 6, "Minute": 0})
    check("RunAtLoad 关闭", d.get("RunAtLoad", False), False)
    check("有工作目录", d["WorkingDirectory"], "/tmp/wd")
    check("有日志路径", "StandardErrorPath" in d, True)


def test_weekday_mapping():
    print("\n星期映射（launchd: 0/7=周日, 1=周一, 2=周二）")
    check("tuesday → 2", SC.WEEKDAYS["tuesday"], 2)
    check("monday → 1", SC.WEEKDAYS["monday"], 1)
    check("sunday → 0", SC.WEEKDAYS["sunday"], 0)


def test_three_plans_from_config():
    print("\n从 config 生成三个任务")
    ps = SC.plans(Cfg(), "/usr/bin/python3", "/tmp/wd")
    labels = [p["label"] for p in ps]
    check("四个任务（日/重试/周/推送）", len(ps), 4)
    check("含 compute-daily", "com.lambo.stockwatch-compute-daily" in labels, True)
    check("含 notify", "com.lambo.stockwatch-notify" in labels, True)
    daily = [p for p in ps if p["label"].endswith("compute-daily")][0]
    check("日报 06:00", daily["calendar"], {"Hour": 6, "Minute": 0})
    notify = [p for p in ps if p["label"].endswith("notify")][0]
    check("推送 08:00", notify["calendar"], {"Hour": 8, "Minute": 0})
    weekly = [p for p in ps if p["label"].endswith("compute-weekly")][0]
    check("周报 周二 06:30", weekly["calendar"],
          {"Weekday": 2, "Hour": 6, "Minute": 30})


def test_no_api_key_leaked_in_cli_mode():
    print("\nclaude_cli 模式下 plist 不能带 API key")
    ps = SC.plans(Cfg(), "/usr/bin/python3", "/tmp/wd")
    for p in ps:
        xml = SC.render_plist(p["label"], p["args"], p["calendar"],
                              "/tmp/wd", env=p.get("env"))
        check(f"{p['label'][-14:]} 无 API key",
              "ANTHROPIC_API_KEY" in xml, False)


if __name__ == "__main__":
    test_plist_is_valid_and_has_calendar()
    test_weekday_mapping()
    test_three_plans_from_config()
    test_no_api_key_leaked_in_cli_mode()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
