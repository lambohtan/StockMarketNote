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


def _hm(s):
    """解析严格的 24 小时制时间；显式非法值必须失败。"""
    try:
        parts = [part.strip() for part in str(s).strip().split(":")]
        if (len(parts) != 2
                or any(not part.isdecimal() or not 1 <= len(part) <= 2
                       for part in parts)):
            raise ValueError
        h, m = (int(x) for x in parts)
    except (TypeError, ValueError):
        raise ValueError(f"非法时间：{s!r}，应为 HH:MM") from None
    if not 0 <= h <= 23 or not 0 <= m <= 59:
        raise ValueError(f"非法时间：{s!r}，小时应为 0-23、分钟应为 0-59")
    return h, m


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
    ph, pm = _hm(cfg.get("schedule.push_time", "08:00"))
    weekday = str(cfg.get("schedule.weekly_day", "tuesday")).strip().lower()
    if weekday not in WEEKDAYS:
        raise ValueError(f"未知星期：{weekday!r}")
    wd = WEEKDAYS[weekday]

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
        env = p.get("env")
        if dry_run and env:
            # 干跑可核对变量名，但不能把 API key 打进终端或日志。
            env = {key: "<redacted>" for key in env}
        xml = render_plist(p["label"], p["args"], p["calendar"],
                           workdir, env=env)
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
        if ok:
            done.append(p["label"])
    return done


def main(argv=None, cfg=None, python_bin=None, workdir=None):
    """CLI 入口；真实安装中任一 bootstrap 失败都返回非零。"""
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in args
    if cfg is None:
        from sw.config import CFG
        cfg = CFG
    root = Path(workdir) if workdir else Path(__file__).resolve().parent.parent
    interpreter = python_bin or sys.executable
    done = apply(cfg, interpreter, str(root), dry_run=dry)
    if not dry:
        expected = {p["label"] for p in plans(cfg, interpreter, str(root))
                    if Path(p["args"][1]).exists()}
        failed = expected - set(done)
        if failed:
            print(f"❌ launchd bootstrap 失败：{', '.join(sorted(failed))}")
            return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
