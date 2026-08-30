#!/usr/bin/env python3
"""plist 渲染测试。运行：python3 tests/test_schedule.py

只测渲染，不真的 load —— 那会改动用户的 LaunchAgents。
"""
import sys, plistlib, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sw import schedule as SC

FAIL = []

def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: 得到 {got!r}，期望 {want!r}")
    if not ok:
        FAIL.append(name)


def check_raises(name, fn, exc=ValueError):
    try:
        fn()
    except exc:
        print(f"  ✅ {name}: 正确抛出 {exc.__name__}")
        return
    except Exception as e:
        print(f"  ❌ {name}: 抛了 {type(e).__name__}，期望 {exc.__name__}")
    else:
        print(f"  ❌ {name}: 没抛异常")
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
    print("\n从 config 生成五个任务")
    ps = SC.plans(Cfg(), "/usr/bin/python3", "/tmp/wd")
    labels = [p["label"] for p in ps]
    check("五个任务（日/重试/周/池/推送）", len(ps), 5)
    check("含 compute-daily", "com.lambo.stockwatch-compute-daily" in labels, True)
    check("含 notify", "com.lambo.stockwatch-notify" in labels, True)
    check("含 pool-daily", "com.lambo.stockwatch-pool-daily" in labels, True)
    daily = [p for p in ps if p["label"].endswith("compute-daily")][0]
    check("日报 06:00", daily["calendar"], {"Hour": 6, "Minute": 0})
    notify = [p for p in ps if p["label"].endswith("notify")][0]
    check("推送 08:00", notify["calendar"], {"Hour": 8, "Minute": 0})
    pool_daily = [p for p in ps if p["label"].endswith("pool-daily")][0]
    check("股票池深读 06:45", pool_daily["calendar"], {"Hour": 6, "Minute": 45})
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


def test_invalid_time_and_weekday_fail_closed():
    print("\n非法时间与星期必须 fail closed")
    for value in ["25:99", "-1:00", "08:60", "8", "noon"]:
        check_raises(f"非法时间 {value!r} 被拒绝",
                     lambda value=value: SC._hm(value))
    for key in ["schedule.compute_daily", "schedule.compute_retry",
                "schedule.compute_weekly", "schedule.push_time"]:
        check_raises(f"{key} 越界值被拒绝",
                     lambda key=key: SC.plans(
                         Cfg(**{key: "25:99"}), "/usr/bin/python3", "/tmp/wd"))
    check_raises("未知 weekday 被拒绝",
                 lambda: SC.plans(Cfg(**{"schedule.weekly_day": "funday"}),
                                   "/usr/bin/python3", "/tmp/wd"))


def test_bootstrap_failure_not_reported_as_success_and_cli_nonzero():
    print("\nlaunchd bootstrap 失败不能算成功，CLI 暴露非零")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name in ["run_daily.py", "run_weekly.py", "run_notify.py"]:
            (root / name).write_text("# test\n", encoding="utf-8")
        agents = root / "LaunchAgents"
        calls = []

        class Result:
            def __init__(self, returncode, stderr=""):
                self.returncode = returncode
                self.stderr = stderr

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == "bootstrap":
                return Result(1, "simulated bootstrap failure")
            return Result(0)

        old_agents, old_run = SC.LAUNCH_AGENTS, SC.subprocess.run
        SC.LAUNCH_AGENTS = agents
        SC.subprocess.run = fake_run
        try:
            done = SC.apply(Cfg(), "/usr/bin/python3", str(root),
                            dry_run=False, skip_missing=False)
        finally:
            SC.LAUNCH_AGENTS, SC.subprocess.run = old_agents, old_run

        check("bootstrap 失败不进入 done", done, [])
        check("五项均尝试 bootstrap",
              sum(1 for argv in calls if argv[1] == "bootstrap"), 5)

        old_apply = SC.apply
        SC.apply = lambda *args, **kwargs: []
        try:
            code = SC.main([], cfg=Cfg(), python_bin="/usr/bin/python3",
                           workdir=str(root))
        finally:
            SC.apply = old_apply
        check("CLI 发现缺失成功项返回非零", code, 1)


if __name__ == "__main__":
    test_plist_is_valid_and_has_calendar()
    test_weekday_mapping()
    test_three_plans_from_config()
    test_no_api_key_leaked_in_cli_mode()
    test_invalid_time_and_weekday_fail_closed()
    test_bootstrap_failure_not_reported_as_success_and_cli_nonzero()
    print("\n" + "=" * 50)
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过: {FAIL}")
        sys.exit(1)
    print("✅ 全部通过")
