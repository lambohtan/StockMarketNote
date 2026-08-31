"""Typed, atomically persisted configuration for the local StockWatch host."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


TASK_NAMES = ("pool_update", "reddit_update", "analysis_update", "publish_report")
WEEKDAYS = tuple(range(7))

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "scheduler": {"enabled": True, "poll_seconds": 15, "catch_up_minutes": 120},
    "tasks": {
        "pool_update": {
            "enabled": True,
            "schedule": {"time": "06:00", "days": list(WEEKDAYS)},
            "parameters": {"top": 20, "min_price": 3.0, "min_market_cap": 300_000_000},
        },
        "reddit_update": {
            "enabled": True,
            "schedule": {"time": "06:30", "days": list(WEEKDAYS)},
            "parameters": {
                "top": 20,
                "window_hours": 168,
                "comments_per_post": 20,
                "min_interval_seconds": 30.0,
                "timeout": 20.0,
                "retention_days": 7,
                "subreddits": "wallstreetbets,stocks,investing",
            },
        },
        "analysis_update": {
            "enabled": False,
            "schedule": {"time": "07:00", "days": [0, 1, 2, 3, 4]},
            "parameters": {
                "top": 10,
                "model": "sonnet",
                "deep": "opus",
                "evidence": "prefetch",
                "tool_rounds": 2,
                "analysts": "market,sentiment,news,fundamentals",
                "rounds": 1,
                "judge_samples": 3,
                "lang": "Simplified Chinese",
            },
        },
        "publish_report": {
            "enabled": True,
            "schedule": {"time": "08:00", "days": [0, 1, 2, 3, 4]},
            "parameters": {"analysis_limit": 20},
        },
    },
}


class ConfigError(ValueError):
    """Configuration is malformed or contains an unsupported value."""


def default_support_root() -> Path:
    override = os.environ.get("STOCKWATCH_SUPPORT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / "StockWatch"


def _number(value: Any, name: str, minimum: float, *, integer: bool = False) -> int | float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} must be a number")
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return parsed


def _clock(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ConfigError(f"{name} must use HH:MM")
    try:
        hour, minute = (int(part) for part in value.split(":"))
    except ValueError as exc:
        raise ConfigError(f"{name} must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ConfigError(f"{name} must use a valid local time")
    return f"{hour:02d}:{minute:02d}"


def _days(value: Any, name: str) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a non-empty list")
    if any(isinstance(day, bool) or not isinstance(day, int) or day not in WEEKDAYS for day in value):
        raise ConfigError(f"{name} must contain weekday numbers 0-6")
    return list(dict.fromkeys(value))


def _enum(value: Any, name: str, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ConfigError(f"{name} must be one of {sorted(allowed)}")
    return value


def _text(value: Any, name: str, *, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ConfigError(f"{name} must be non-empty text up to {maximum} characters")
    if any(ord(char) < 32 and char not in "\t" for char in value):
        raise ConfigError(f"{name} contains control characters")
    return value.strip()


def validate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ConfigError("configuration must be an object")
    unknown = set(raw) - {"schema_version", "scheduler", "tasks"}
    if unknown:
        raise ConfigError(f"unknown top-level keys: {sorted(unknown)}")
    if raw.get("schema_version", 1) != 1:
        raise ConfigError("unsupported schema_version")

    scheduler = raw.get("scheduler")
    tasks = raw.get("tasks")
    if not isinstance(scheduler, Mapping) or not isinstance(tasks, Mapping):
        raise ConfigError("scheduler and tasks must be objects")
    if set(tasks) != set(TASK_NAMES):
        raise ConfigError(f"tasks must be exactly {list(TASK_NAMES)}")

    validated: dict[str, Any] = {
        "schema_version": 1,
        "scheduler": {
            "enabled": bool(scheduler.get("enabled", True)),
            "poll_seconds": _number(
                scheduler.get("poll_seconds", 15), "scheduler.poll_seconds", 1, integer=True
            ),
            "catch_up_minutes": _number(
                scheduler.get("catch_up_minutes", 120),
                "scheduler.catch_up_minutes",
                0,
                integer=True,
            ),
        },
        "tasks": {},
    }

    for task_name in TASK_NAMES:
        task = tasks[task_name]
        if not isinstance(task, Mapping):
            raise ConfigError(f"tasks.{task_name} must be an object")
        schedule = task.get("schedule")
        params = task.get("parameters")
        if not isinstance(schedule, Mapping) or not isinstance(params, Mapping):
            raise ConfigError(f"tasks.{task_name}.schedule/parameters must be objects")
        validated["tasks"][task_name] = {
            "enabled": bool(task.get("enabled", False)),
            "schedule": {
                "time": _clock(schedule.get("time"), f"tasks.{task_name}.schedule.time"),
                "days": _days(schedule.get("days"), f"tasks.{task_name}.schedule.days"),
            },
            "parameters": _validate_parameters(task_name, params),
        }
    return validated


def _validate_parameters(task_name: str, params: Mapping[str, Any]) -> dict[str, Any]:
    expected = set(DEFAULT_CONFIG["tasks"][task_name]["parameters"])
    unknown = set(params) - expected
    missing = expected - set(params)
    if unknown or missing:
        raise ConfigError(
            f"tasks.{task_name}.parameters mismatch; missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    name = f"tasks.{task_name}.parameters"
    if task_name == "pool_update":
        return {
            "top": _number(params["top"], f"{name}.top", 1, integer=True),
            "min_price": _number(params["min_price"], f"{name}.min_price", 0),
            "min_market_cap": _number(params["min_market_cap"], f"{name}.min_market_cap", 0),
        }
    if task_name == "reddit_update":
        return {
            "top": _number(params["top"], f"{name}.top", 1, integer=True),
            "window_hours": _number(
                params["window_hours"], f"{name}.window_hours", 1, integer=True
            ),
            "comments_per_post": _number(
                params["comments_per_post"], f"{name}.comments_per_post", 1, integer=True
            ),
            "min_interval_seconds": _number(
                params["min_interval_seconds"], f"{name}.min_interval_seconds", 0
            ),
            "timeout": _number(params["timeout"], f"{name}.timeout", 0.1),
            "retention_days": _number(
                params["retention_days"], f"{name}.retention_days", 1, integer=True
            ),
            "subreddits": _text(params["subreddits"], f"{name}.subreddits"),
        }
    if task_name == "analysis_update":
        return {
            "top": _number(params["top"], f"{name}.top", 1, integer=True),
            "model": _text(params["model"], f"{name}.model", maximum=40),
            "deep": _text(params["deep"], f"{name}.deep", maximum=40),
            "evidence": _enum(
                params["evidence"], f"{name}.evidence", {"prefetch", "hybrid", "tools"}
            ),
            "tool_rounds": _number(
                params["tool_rounds"], f"{name}.tool_rounds", 0, integer=True
            ),
            "analysts": _text(params["analysts"], f"{name}.analysts"),
            "rounds": _number(params["rounds"], f"{name}.rounds", 1, integer=True),
            "judge_samples": _number(
                params["judge_samples"], f"{name}.judge_samples", 1, integer=True
            ),
            "lang": _text(params["lang"], f"{name}.lang", maximum=80),
        }
    return {
        "analysis_limit": _number(
            params["analysis_limit"], f"{name}.analysis_limit", 1, integer=True
        )
    }


class ConfigManager:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.save(DEFAULT_CONFIG)

    def load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read {self.path}: {exc}") from exc
        return validate_config(payload)

    def save(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        validated = validate_config(payload)
        descriptor, temporary = tempfile.mkstemp(prefix="config-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(validated, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return copy.deepcopy(validated)

