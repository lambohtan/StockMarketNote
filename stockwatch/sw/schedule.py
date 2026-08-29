"""
launchd 任务的渲染与加载。

⚠️ 必须用 launchd 不能用 cron —— macOS 休眠时 cron 会静默跳过任务，
不补跑也不报错。launchd 的 StartCalendarInterval 在唤醒后会补跑。

⚠️ StartCalendarInterval 按**本机墙钟时间**触发，所以用户换时区、出差，
推送时间自动跟随。这正是「读取系统时区」的需求，不需要额外代码。

本模块被 CLI 和以后的设置窗口**共用同一个函数** —— 不要在做 UI 时另写一份。
"""
import os
import plistlib
import subprocess
from pathlib import Path

LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PREFIX = "com.lambo.stockwatch"

# launchd 约定：0 和 7 都是周日，1 是周一
WEEKDAYS = {"sunday": 0, "monday": 1, "tuesday": 2, "wednesday": 3,
            "thursday": 4, "friday": 5, "saturday": 6}


def _hm(s, default=(6, 0)):
    try:
        h, m = str(s).split(":")
        return int(h), int(m)
    except Exception:
        return default


def render_plist(label, args, calendar, workdir, env=None, logs_dir=None):
    """生成一个 launchd plist 的 XML 文本。"""
    logs = Path(logs_dir) if logs_dir else Path(workdir) / "logs"
    d = {
        "Label": label,
        "ProgramArguments": list(args),
        "StartCalendarInterval": dict(calendar),
        "RunAtLoad": False,
        "WorkingDirectory": str(workdir),
        "StandardOutPath": str(logs / f"{label}.out.log"),
        "StandardErrorPath": str(logs / f"{label}.err.log"),
        "ProcessType": "Background",
    }
    if env:
        d["EnvironmentVariables"] = dict(env)
    return plistlib.dumps(d).decode("utf-8")


def plans(cfg, python_bin, workdir):
    """从 config 生成全部任务定义。"""
    dh, dm = _hm(cfg.get("schedule.compute_daily", "06:00"))
    rh, rm = _hm(cfg.get("schedule.compute_retry", "07:00"))
    wh, wm = _hm(cfg.get("schedule.compute_weekly", "06:30"))
    ph, pm = _hm(cfg.get("schedule.push_time", "08:00"), (8, 0))
    wd = WEEKDAYS.get(str(cfg.get("schedule.weekly_day", "tuesday")).lower(), 2)

    env = None
    # provider=api 时才把 key 注进任务环境；claude_cli 模式下绝不能带，
    # 否则 Claude Code 会静默改用 API 计费而不是订阅。
    if cfg.get("llm.provider", "claude_cli") == "api":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if key:
            env = {"ANTHROPIC_API_KEY": key}

    return [
        {"label": f"{PREFIX}-compute-daily",
         "args": [python_bin, str(Path(workdir) / "run_daily.py")],
         "calendar": {"Hour": dh, "Minute": dm}, "env": env},
        {"label": f"{PREFIX}-compute-retry",
         "args": [python_bin, str(Path(workdir) / "run_daily.py")],
         "calendar": {"Hour": rh, "Minute": rm}, "env": env},
        {"label": f"{PREFIX}-compute-weekly",
         "args": [python_bin, str(Path(workdir) / "run_weekly.py")],
         "calendar": {"Weekday": wd, "Hour": wh, "Minute": wm}, "env": env},
        {"label": f"{PREFIX}-notify",
         "args": [python_bin, str(Path(workdir) / "run_notify.py")],
         "calendar": {"Hour": ph, "Minute": pm}, "env": None},
    ]


def apply(cfg, python_bin, workdir, dry_run=False, skip_missing=True):
    """写 plist 并重载。返回已处理的 label 列表。"""
    if not dry_run:
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "logs").mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    done = []
    for p in plans(cfg, python_bin, workdir):
        script = Path(p["args"][1])
        # 干跑要完整展示计划，方便核对即将安装的全部 plist；真安装时才
        # 跳过尚未创建的脚本（当前 weekly 入口可能尚未存在）。
        if skip_missing and not dry_run and not script.exists():
            print(f"  ⏭  {p['label']}：{script.name} 还不存在，跳过")
            continue
        xml = render_plist(p["label"], p["args"], p["calendar"],
                           workdir, env=p.get("env"))
        target = LAUNCH_AGENTS / f"{p['label']}.plist"
        if dry_run:
            print(f"--- {target}\n{xml}")
            done.append(p["label"])
            continue
        target.write_text(xml, encoding="utf-8")
        # bootout 允许失败（首次安装时本来就没加载过）
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{p['label']}"],
                       capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(target)],
                           capture_output=True, text=True)
        ok = r.returncode == 0
        print(f"  {'✅' if ok else '❌'} {p['label']} "
              f"{p['calendar']}{'' if ok else '  ' + r.stderr.strip()[:120]}")
        done.append(p["label"])
    return done


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from sw.config import CFG
    dry = "--dry-run" in sys.argv
    apply(CFG, sys.executable, str(Path(__file__).resolve().parent.parent),
          dry_run=dry)
