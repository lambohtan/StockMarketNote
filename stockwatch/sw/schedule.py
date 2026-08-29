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
    provider = str(cfg.get("llm.provider", "claude_cli")).strip()
    if provider not in ("claude_cli", "api"):
        raise ValueError(f"未知的 llm.provider：{provider!r}")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if provider == "api" and not api_key:
        # 在任何 plist/launchctl 变更前 fail closed。
        raise ValueError("llm.provider=api 但没有 ANTHROPIC_API_KEY，拒绝安装")
    if provider == "claude_cli" and api_key:
        raise ValueError("llm.provider=claude_cli 但环境存在 ANTHROPIC_API_KEY，拒绝安装")
    dh, dm = _hm(cfg.get("schedule.compute_daily", "06:00"))
    rh, rm = _hm(cfg.get("schedule.compute_retry", "07:00"))
    wh, wm = _hm(cfg.get("schedule.compute_weekly", "06:30"))
    lh, lm = _hm(cfg.get("schedule.pool_daily", "06:45"))
    ph, pm = _hm(cfg.get("schedule.push_time", "08:00"))
    weekday = str(cfg.get("schedule.weekly_day", "tuesday")).strip().lower()
    if weekday not in WEEKDAYS:
        raise ValueError(f"未知星期：{weekday!r}")
    wd = WEEKDAYS[weekday]

    env = None
    # provider=api 时才把 key 注进任务环境；claude_cli 模式下绝不能带，
    # 否则 Claude Code 会静默改用 API 计费而不是订阅。
    if provider == "api":
        env = {"ANTHROPIC_API_KEY": api_key}

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
        {"label": f"{PREFIX}-pool-daily",
         "args": [python_bin, str(Path(workdir) / "run_pool.py")],
         "calendar": {"Hour": lh, "Minute": lm}, "env": env},
        {"label": f"{PREFIX}-notify",
         "args": [python_bin, str(Path(workdir) / "run_notify.py")],
         "calendar": {"Hour": ph, "Minute": pm}, "env": None},
    ]


def apply(cfg, python_bin, workdir, dry_run=False, skip_missing=True):
    """写 plist 并重载。返回已处理的 label 列表。"""
    # 先完成全部配置校验；API 缺 key 等拒绝路径不能连日志目录都创建。
    planned = plans(cfg, python_bin, workdir)
    if not dry_run:
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "logs").mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    done = []
    for p in planned:
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
        # 先以临时文件原子替换，并保留旧 plist；bootstrap 失败时文件和服务
        # 都回到原状态。所有生成的 plist 固定 0600，防止未来误含 secret。
        old_exists = target.exists()
        old_bytes = target.read_bytes() if old_exists else None
        old_mode = target.stat().st_mode & 0o777 if old_exists else None
        old_loaded = False
        if old_exists:
            # 仅查询旧服务状态；回滚时只恢复原本已加载的 job，避免一个原先
            # 未加载的 plist 因失败回滚而被意外启动。
            state = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{p['label']}"],
                capture_output=True, text=True)
            old_loaded = getattr(state, "returncode", 1) == 0
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        temp.write_text(xml, encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, target)
        # bootout 允许失败（首次安装时本来就没加载过）
        error_detail = ""
        try:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{p['label']}"],
                           capture_output=True)
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(target)],
                               capture_output=True, text=True)
            ok = r.returncode == 0
            error_detail = getattr(r, "stderr", "") or ""
        except Exception as exc:
            ok = False
            error_detail = f"{type(exc).__name__}: {exc}"
        print(f"  {'✅' if ok else '❌'} {p['label']} "
              f"{p['calendar']}{'' if ok else '  ' + error_detail.strip()[:120]}")
        if ok:
            os.chmod(target, 0o600)
            done.append(p["label"])
        else:
            # 恢复旧 plist，并尝试恢复之前加载的服务。即使恢复 bootstrap
            # 失败，也不能把新失败误报为成功。
            try:
                if old_exists:
                    restore = target.with_name(f".{target.name}.{os.getpid()}.restore")
                    restore.write_bytes(old_bytes)
                    os.chmod(restore, old_mode or 0o600)
                    os.replace(restore, target)
                else:
                    target.unlink(missing_ok=True)
            finally:
                if old_exists and old_loaded:
                    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}/{p['label']}",
                                    str(target)], capture_output=True, text=True)
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
    try:
        planned = plans(cfg, interpreter, str(root))
        done = apply(cfg, interpreter, str(root), dry_run=dry)
    except ValueError as exc:
        print(f"❌ 调度配置拒绝安装：{exc}")
        return 1
    if not dry:
        expected = {p["label"] for p in planned
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
