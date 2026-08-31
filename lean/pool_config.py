"""Runtime configuration for the candidate pool stage.

Pool size, screening floors and source weights change often, so they live in a
plain JSON file that can be edited at any time without touching code.  Because
the store keeps the *entire* ranking of every run, changing ``top_n`` takes
effect immediately on pools that were already built — no rebuild needed.

Precedence, highest first: explicit CLI overrides, environment variables, the
config file, then the built-in defaults.  Unknown keys and unusable values are
rejected loudly instead of being silently ignored.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from lean.pool import DEFAULT_WEIGHTS

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_CONFIG_NAME = "pool.json"
DEFAULT_DB_PATH = REPO_ROOT / "local-data" / "pool" / "pool.sqlite3"

ENV_PREFIX = "POOL_"
_ENV_KEYS = {
    "POOL_TOP_N": "top_n",
    "POOL_MIN_PRICE": "min_price",
    "POOL_MIN_MARKET_CAP": "min_market_cap",
    "POOL_KEEP_RUNS": "keep_runs",
    "POOL_MAX_AGE_HOURS": "max_age_hours",
    "POOL_DB_PATH": "db_path",
}
_FIELDS = ("top_n", "min_price", "min_market_cap", "keep_runs", "max_age_hours",
           "db_path", "weights")


class PoolConfigError(ValueError):
    """The configuration file, environment or override is unusable."""


@dataclass(frozen=True)
class PoolConfig:
    top_n: int = 20
    min_price: float = 3.0
    min_market_cap: float = 3e8
    keep_runs: int = 30
    max_age_hours: float = 36.0
    db_path: Path = DEFAULT_DB_PATH
    weights: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    source: str = "built-in defaults"

    def to_dict(self) -> dict:
        return {
            "top_n": self.top_n,
            "min_price": self.min_price,
            "min_market_cap": self.min_market_cap,
            "keep_runs": self.keep_runs,
            "max_age_hours": self.max_age_hours,
            "db_path": str(self.db_path),
            "weights": dict(self.weights),
            "source": self.source,
        }


def _positive_int(name: str, raw: object, minimum: int = 1) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise PoolConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise PoolConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _number(name: str, raw: object, minimum: float = 0.0) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise PoolConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise PoolConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _weights(raw: object) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        raise PoolConfigError(f"weights must be an object, got {raw!r}")
    merged = dict(DEFAULT_WEIGHTS)
    for source, value in raw.items():
        if source not in DEFAULT_WEIGHTS:
            raise PoolConfigError(
                f"unknown weight source {source!r}; known: {sorted(DEFAULT_WEIGHTS)}")
        merged[source] = _number(f"weights.{source}", value)
    return merged


def _read_file(path: Path, explicit: bool) -> dict:
    if not path.is_file():
        if explicit:
            raise PoolConfigError(f"config file not found: {path}")
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PoolConfigError(f"cannot read config file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PoolConfigError(f"config file {path} must contain a JSON object")
    unknown = sorted(set(payload) - set(_FIELDS))
    if unknown:
        raise PoolConfigError(f"unknown config keys in {path}: {unknown}")
    return payload


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None,
                overrides: Mapping[str, object] | None = None,
                config_root: Path | None = None) -> PoolConfig:
    """Resolve pool configuration from file, environment and explicit overrides."""

    env = os.environ if env is None else env
    explicit = path is not None or bool(env.get("POOL_CONFIG_PATH"))
    resolved = Path(path or env.get("POOL_CONFIG_PATH")
                    or (config_root or DEFAULT_CONFIG_DIR) / DEFAULT_CONFIG_NAME)

    values: dict[str, object] = dict(_read_file(resolved, explicit))
    layers = [str(resolved)] if values else ["built-in defaults"]

    for env_key, field_name in _ENV_KEYS.items():
        if env.get(env_key) not in (None, ""):
            values[field_name] = env[env_key]
            if "env" not in layers:
                layers.append("env")

    for key, value in (overrides or {}).items():
        if key not in _FIELDS:
            raise PoolConfigError(f"unknown config override {key!r}")
        if value is not None:
            values[key] = value
            if "cli" not in layers:
                layers.append("cli")

    return PoolConfig(
        top_n=_positive_int("top_n", values.get("top_n", 20)),
        min_price=_number("min_price", values.get("min_price", 3.0)),
        min_market_cap=_number("min_market_cap", values.get("min_market_cap", 3e8)),
        keep_runs=_positive_int("keep_runs", values.get("keep_runs", 30)),
        max_age_hours=_number("max_age_hours", values.get("max_age_hours", 36.0), minimum=0.0),
        db_path=Path(str(values.get("db_path", DEFAULT_DB_PATH))).expanduser(),
        weights=_weights(values["weights"]) if "weights" in values else dict(DEFAULT_WEIGHTS),
        source=" + ".join(layers),
    )
